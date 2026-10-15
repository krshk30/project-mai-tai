import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from project_mai_tai import events
from project_mai_tai.events import MarketDataSubscriptionEvent, QuoteTickEvent
from project_mai_tai.market_data.models import QuoteTickRecord
from tests.unit.test_momlive1_runtime import event, offer_and_drain, setup
from tests.unit.test_momwarn1_gateway import event as subscription, service as gateway_service


@pytest.mark.asyncio
@pytest.mark.parametrize("late,deferred,missing", [
    (False, False, False), (True, False, False),
    (False, True, False), (True, True, False), (False, False, True),
])
async def test_actual_warning_t_only_owner_requests_targeted_q_before_single_bounded_wire(late, deferred, missing, monkeypatch):
    now, broker, oms, runtime = setup()
    monkeypatch.setattr(events, "datetime", SimpleNamespace(now=lambda zone: now[0]))
    gateway, provider, redis, _ = gateway_service()
    gateway.settings.market_data_warmup_enabled = False
    await gateway.apply_subscription_event(subscription("momentum-warning", ["SCNI"]))
    assert gateway._regular_symbols(gateway._desired_symbols_by_consumer) == set()
    assert "Q.SCNI" not in provider._ws.added
    assert "T.SCNI" in provider._ws.added
    oms._latest_quotes_by_symbol.clear()
    original_quote_write = redis.xadd
    async def quote_write(stream, fields, **kwargs):
        result = await original_quote_write(stream, fields, **kwargs)
        payload = json.loads(fields["data"])
        if payload.get("event_type") == "quote_tick":
            await oms._handle_quote_tick_event(QuoteTickEvent.model_validate(payload))
        return result
    redis.xadd = quote_write
    requested = []
    deliveries = []
    async def deliver():
        if deferred:
            await asyncio.sleep(.01)
        if late:
            now[0] += timedelta(seconds=2, microseconds=1)
        await gateway._publish_quote_tick_safely(QuoteTickRecord("SCNI", 3, 3.001))
    async def command_write(stream, fields, **kwargs):
        if stream.endswith(":market-data-subscriptions"):
            command = MarketDataSubscriptionEvent.model_validate_json(fields["data"])
            requested.append(command)
            await gateway.apply_subscription_event(command)
            if command.payload.symbols:
                assert runtime.book._active_count == 1 and broker.sent == []
                assert next(iter(runtime.book.episodes.values())).first_submit_ns is None
                if missing:
                    pass
                elif deferred:
                    deliveries.append(asyncio.create_task(deliver()))
                else:
                    await deliver()
        return "1-0"
    oms.redis.xadd.side_effect = command_write
    await runtime.start()
    oms._momentum_live_quote_dispatch = runtime.actor.dispatch
    try:
        await offer_and_drain(runtime, event(now[0]))
        buys = [row for row in broker.sent if row.side == "buy"]
        assert len(buys) == (0 if late or missing else 1)
        assert any(command.payload.consumer_name == "momentum-live" and command.payload.symbols == ["SCNI"]
                   for command in requested)
        assert not any("*" in topic for topic in provider._ws.added)
        if late or missing:
            assert runtime.book._active_count == 0
            assert next(iter(runtime.book.episodes.values())).state == "terminal_no_fill"
            assert next(iter(runtime.book.episodes.values())).first_submit_ns is None
            assert runtime.actor.dispatch.quote_waiters == {}
        else:
            assert buys[0].metadata["limit_price"] == "3.01" and buys[0].quantity == 33
            assert "Q.SCNI" in provider._ws.added
    finally:
        await asyncio.gather(*deliveries)
        await runtime.close()
