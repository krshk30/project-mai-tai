import json
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import BrokerOrder, TradeIntent
from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, Signal, STRATEGY_CODE
from project_mai_tai.momentum_live.owner_proof import owner_proof
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from tests.unit.test_momlive1_runtime import event, offer_and_drain, setup
from tests.unit.momlive1_owned_fixtures import complete_owned_observation


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [None, "working", "corrupt"])
async def test_next_day_off_empty_book_clears_stale_owner_only_after_actual_empty_db_proof(unsafe):
    now, broker, service, runtime = setup()
    await runtime.start()
    await offer_and_drain(runtime, event(now[0]))
    await complete_owned_observation(runtime, next(iter(runtime.book.episodes)))
    episode = next(iter(runtime.book.episodes.values()))
    old = owner_proof(runtime.book, observed_ns=runtime.now_ns(), producer_instance_id="123:456",
        account_names=runtime.accounts, bindings=runtime.bindings, entry_admission_enabled=True)
    await runtime.close()
    with service.session_factory() as session:
        exit_order = OrderRequest("controlled-terminal-exit", "live:orb", STRATEGY_CODE, "SCNI", "sell", "close",
            D(33), "native-target", {"episode_id": episode.episode_id}, "limit", "day")
        runtime.db.prepare_order(session, episode.episode_id, exit_order, provider="webull")
        runtime.db.record(session, exit_order, [ExecutionReport("filled", exit_order.client_order_id,
            filled_quantity=D(33), fill_price=D("3.15"), origin="broker", reported_at=now[0],
            metadata={"momlive_execution_time_source": "broker_execution_time"})])
        for row in session.scalars(select(BrokerOrder)).all():
            row.status = "cancelled" if row.status in {"pending", "accepted", "partially_filled"} else row.status
        runtime.book.reconcile(episode.episode_id, cumulative_filled=D(33), cumulative_exited=D(33),
            now_ns=runtime.now_ns(), terminal_entry=True)
        runtime.db.save(session, runtime.book.snapshot()[0])
        # The runtime and durable lifecycle rows must share the owned Oct14 clock.
        for model in (TradeIntent, BrokerOrder):
            for row in session.scalars(select(model)).all():
                row.created_at = row.updated_at = now[0]
        if unsafe == "working":
            session.scalars(select(BrokerOrder)).first().status = "accepted"
        elif unsafe == "corrupt":
            parent = session.get(TradeIntent, UUID(episode.episode_id))
            parent.payload = {**parent.payload, "momlive_episode": {**parent.payload["momlive_episode"], "filled": "999"}}
        session.commit()
    now[0] += timedelta(days=1)
    service.settings.momentum_live_enabled = False
    service.redis.xadd.reset_mock()
    service.redis.xrevrange = AsyncMock(return_value=[("1-0", {"data": json.dumps(old)})])
    sent_before = len(broker.sent)
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="987:654")
    if unsafe:
        with pytest.raises(Refusal):
            await restarted.start()
        service.redis.xadd.assert_not_awaited()
    else:
        assert await restarted.start() is False
        commands = [json.loads(call.args[1]["data"]) for call in service.redis.xadd.await_args_list]
        assert commands[0]["payload"]["consumer_name"] == "momentum-live"
        assert commands[0]["payload"]["symbols"] == []
        proof = commands[1]
        assert proof["producer_instance_id"] == "987:654" and proof["episodes"] == []
        assert proof["restore_ready"] and not proof["entry_admission_enabled"]
    assert len(broker.sent) == sent_before


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_order", [False, True])
async def test_boot_releases_only_proven_unsent_quote_claim_and_never_reissues(durable_order):
    now, broker, service, runtime = setup()
    raw = json.loads(event(now[0]).payload.metadata["momlive_signal"])
    signal = Signal(**{**raw, "signal_price": D(raw["signal_price"])})
    runtime.book = EpisodeLedger.restore([])
    episode, fresh = runtime.book.claim(signal, now_ns=runtime.now_ns())
    assert fresh
    with service.session_factory() as session:
        assert runtime.db.claim(session, episode=runtime.book.snapshot()[0],
            account="live:orb", provider="webull", signal=raw)
        if durable_order:
            request = OrderRequest("unknown-prepared-order", "live:orb", STRATEGY_CODE,
                "SCNI", "buy", "open", D(33), "controlled",
                {"episode_id": episode.episode_id}, "limit", "day")
            runtime.db.prepare_order(session, episode.episode_id, request, provider="webull")
        session.commit()
    restarted = OmsLiveRuntime(service, clock=lambda: now[0], identity="987:654")
    if durable_order:
        with pytest.raises(Refusal, match="boot_quote_reservation_order_or_exposure_UNKNOWN"):
            await restarted.start()
        service.redis.xadd.assert_not_awaited()
    else:
        assert await restarted.start()
        try:
            assert restarted.book._active_count == 0
            assert restarted.book.episodes[episode.episode_id].state == "terminal_no_fill"
            await offer_and_drain(restarted, event(now[0]))
            assert restarted.book._active_count == 0
        finally:
            await restarted.close()
    assert broker.sent == []
