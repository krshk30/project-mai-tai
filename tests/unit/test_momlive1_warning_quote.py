"""Warning-time Q preparation through existing owners; no network or clock bypass."""
import asyncio
from datetime import timedelta
from decimal import Decimal as D
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from project_mai_tai import events
from project_mai_tai.events import MarketDataSubscriptionEvent, QuoteTickEvent
from project_mai_tai.market_data.models import QuoteTickRecord
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.positions_read_receipt import record_receipts
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momlive1_runtime import event, offer_and_drain, setup
from tests.unit.test_momwarn1_gateway import event as subscription, service as gateway_service
from tests.unit.test_momwarn1_no_data_expiry import admitted, expire
from tests.unit.test_momentum_paper_service import _store
from tests.unit.test_momentum_warning import START


def paper(enabled=True):
    service = MomentumPaperService(Settings(_env_file=None, momentum_live_enabled=enabled),
                                   redis_client=AsyncMock(), store=_store())
    service._engine = MomentumPaperEngine(prior_closes={"TEST": D(10)}, condition_version="fixture",
                                          coverage_started_ms=START)
    service._warning = admitted()
    return service


def commands(service):
    return [MarketDataSubscriptionEvent.model_validate_json(call.args[1]["data"])
            for call in service.redis.xadd.await_args_list
            if call.args[0].endswith(":market-data-subscriptions")]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_warning_primes_existing_paper_q_only_when_live_enabled(enabled):
    service = paper(enabled)
    await service._sync_gateway_subscriptions()
    owners = {item.payload.consumer_name: item.payload.symbols for item in commands(service)}
    assert owners == {"momentum-warning": ["TEST"], "momentum-paper": ["TEST"] if enabled else []}
    assert service._subscribed_symbols == ({"TEST"} if enabled else set())


@pytest.mark.asyncio
@pytest.mark.parametrize("held_elsewhere", [True, False])
async def test_warning_drop_releases_immediately_but_never_clears_an_oms_held_owner(held_elsewhere):
    service = paper()
    gateway, provider, _, _ = gateway_service()
    gateway.settings.market_data_warmup_enabled = False
    if held_elsewhere:
        await gateway.apply_subscription_event(subscription("momentum-live", ["TEST"]))
    await service._sync_gateway_subscriptions()
    for item in commands(service):
        await gateway.apply_subscription_event(item)
    assert "Q.TEST" in provider._ws.added
    service.redis.xadd.reset_mock()
    assert expire(service._warning, 61_000)
    # Same paper clock instant: no debounce may postpone a dropped warning's Q release.
    await service._sync_gateway_subscriptions()
    assert {item.payload.consumer_name: item.payload.symbols for item in commands(service)} == {
        "momentum-warning": [], "momentum-paper": []}
    for item in commands(service):
        await gateway.apply_subscription_event(item)
    assert gateway._regular_symbols(gateway._desired_symbols_by_consumer) == ({"TEST"} if held_elsewhere else set())
    assert ("Q.TEST" in provider._ws.removed) is not held_elsewhere


@pytest.mark.asyncio
async def test_force_shutdown_clears_paper_without_clearing_warning_or_live_foreign_owners():
    service = paper()
    await service._sync_gateway_subscriptions()
    service.redis.xadd.reset_mock()
    await service._sync_gateway_subscriptions(force=True, desired_override=set())
    assert {c.payload.consumer_name: c.payload.symbols for c in commands(service)} == {
        "momentum-warning": [], "momentum-paper": []}
    assert not any(c.payload.consumer_name == "momentum-live" for c in commands(service))


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [16, 17])
async def test_existing_shared_cap_is_not_expanded(count):
    service = paper()
    symbols = {"S" + chr(65+i) for i in range(count)}
    service._warning = SimpleNamespace(desired_symbols=lambda active: symbols | active)
    if count > 16:
        with pytest.raises(RuntimeError, match="shared subscription cap"):
            await service._sync_gateway_subscriptions()
        assert not commands(service)
    else:
        await service._sync_gateway_subscriptions()
        assert all(len(item.payload.symbols) == 16 for item in commands(service))


@pytest.mark.asyncio
async def test_inflight_warning_add_cannot_overwrite_a_later_quote_drop():
    service = paper()
    started, release = asyncio.Event(), asyncio.Event()
    applied = []
    async def write(stream, fields, **kwargs):
        if stream.endswith(":market-data-subscriptions"):
            item = MarketDataSubscriptionEvent.model_validate_json(fields["data"])
            if item.payload.consumer_name == "momentum-paper":
                if item.payload.symbols:
                    started.set()
                    await release.wait()
                applied.append(item.payload.symbols)
        return "1-0"
    service.redis.xadd.side_effect = write
    add = asyncio.create_task(service._sync_gateway_subscriptions())
    await asyncio.wait_for(started.wait(), 1)
    assert expire(service._warning, 61_000)
    drop = asyncio.create_task(service._sync_gateway_subscriptions())
    release.set()
    await asyncio.wait_for(asyncio.gather(add, drop), 1)
    assert applied == [["TEST"], []]


@pytest.mark.asyncio
async def test_full_cap_rotation_never_publishes_a_transient_seventeenth_symbol():
    service = paper()
    old = {"S" + chr(65+i) for i in range(16)}
    new = (old - {"SA"}) | {"ZZ"}
    desired = [old]
    service._warning = SimpleNamespace(desired_symbols=lambda active: desired[0] | active)
    owners = {"momentum-paper": set(), "momentum-warning": set()}
    steps = []
    async def write(stream, fields, **kwargs):
        if stream.endswith(":market-data-subscriptions"):
            item = MarketDataSubscriptionEvent.model_validate_json(fields["data"])
            owners[item.payload.consumer_name] = set(item.payload.symbols)
            steps.append((item.payload.consumer_name, set(item.payload.symbols)))
            assert len(set.union(*owners.values())) <= 16
        return "1-0"
    service.redis.xadd.side_effect = write
    await service._sync_gateway_subscriptions()
    steps.clear()
    desired[0] = new
    await service._sync_gateway_subscriptions()
    assert steps == [("momentum-paper", old & new), ("momentum-warning", new), ("momentum-paper", new)]
    assert owners == {"momentum-paper": new, "momentum-warning": new}


@pytest.mark.asyncio
async def test_unknown_expansion_ack_requires_contraction_before_next_full_cap_rotation():
    service = paper()
    old = {"S" + chr(65+i) for i in range(16)}
    desired = [old]
    service._warning = SimpleNamespace(desired_symbols=lambda active: desired[0] | active)
    owners = {"momentum-paper": set(), "momentum-warning": set()}
    fail_ack = [False]
    async def write(stream, fields, **kwargs):
        if stream.endswith(":market-data-subscriptions"):
            item = MarketDataSubscriptionEvent.model_validate_json(fields["data"])
            owners[item.payload.consumer_name] = set(item.payload.symbols)
            assert len(set.union(*owners.values())) <= 16
            if fail_ack[0] and item.payload.consumer_name == "momentum-paper" and len(item.payload.symbols) == 16:
                fail_ack[0] = False
                raise TimeoutError("Applied expansion with missing acknowledgement")
        return "1-0"
    service.redis.xadd.side_effect = write
    await service._sync_gateway_subscriptions()
    desired[0] = (old - {"SA"}) | {"ZZ"}
    fail_ack[0] = True
    with pytest.raises(TimeoutError):
        await service._sync_gateway_subscriptions()
    assert service._gateway_subscription_uncertain
    desired[0] = (old - {"SA"}) | {"YY"}
    await service._sync_gateway_subscriptions()
    assert owners == {"momentum-paper": desired[0], "momentum-warning": desired[0]}
    assert not service._gateway_subscription_uncertain


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14])
async def test_cached_warning_quote_enters_within_two_seconds_without_signal_warmup(hour, monkeypatch):
    now, broker, oms, runtime = setup()
    now[0] = now[0].replace(hour=hour)
    with oms.session_factory() as session:
        record_receipts(session, [(name, now[0], 0) for name in runtime.accounts], recorded_at=now[0])
        session.commit()
    oms._latest_quotes_by_symbol.clear()
    runtime._wait_quote = AsyncMock(side_effect=AssertionError("A prepared quote must not wait at the signal"))
    monkeypatch.setattr(events, "datetime", SimpleNamespace(now=lambda zone: now[0]))
    gateway, provider, redis, _ = gateway_service()
    gateway._publish_historical_warmup = AsyncMock()
    service = paper()
    service._warning = SimpleNamespace(desired_symbols=lambda active: {"SCNI"} | active)
    await service._sync_gateway_subscriptions()
    for command in commands(service):
        await gateway.apply_subscription_event(command)
    assert "Q.SCNI" in provider._ws.added
    assert gateway._publish_historical_warmup.await_count == 1  # At WARNING only.
    write = redis.xadd
    async def quote_write(stream, fields, **kwargs):
        result = await write(stream, fields, **kwargs)
        payload = json.loads(fields["data"])
        if payload.get("event_type") == "quote_tick":
            await oms._handle_quote_tick_event(QuoteTickEvent.model_validate(payload))
        return result
    redis.xadd = quote_write
    await gateway._publish_quote_tick_safely(QuoteTickRecord("SCNI", 3, 3.001))
    first_quote = now[0]
    now[0] += timedelta(milliseconds=100)
    async def owner_write(stream, fields, **kwargs):
        if stream.endswith(":market-data-subscriptions"):
            await gateway.apply_subscription_event(MarketDataSubscriptionEvent.model_validate_json(fields["data"]))
        return "1-0"
    oms.redis.xadd.side_effect = owner_write
    await runtime.start()
    try:
        await offer_and_drain(runtime, event(now[0]))
        buys = [request for request in broker.sent if request.side == "buy"]
        assert len(buys) == 1
        assert (now[0] - first_quote).total_seconds() < 2
        assert gateway._publish_historical_warmup.await_count == 1
        runtime._wait_quote.assert_not_awaited()
    finally:
        await runtime.close()
