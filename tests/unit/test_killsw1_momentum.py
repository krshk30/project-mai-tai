from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from tests.unit.test_momlive1_actor import setup as actor_setup
from tests.unit.test_momlive1_soft_exit_chase import exit_orders, prepared


@pytest.mark.asyncio
async def test_stop_cancels_partial_entry_before_any_forced_exit():
    actor, ports, episode, position = actor_setup(filled=3, now=1_100_000_000)
    ports.stop_requested = lambda: True
    calls = []
    ports.protect_owned.side_effect = lambda owner: calls.append(("protect", owner.quantity))
    ports.cancel_entry_once.side_effect = lambda identity: calls.append(("cancel", identity))
    await actor.restore_before_serving()
    assert calls == [("protect", Decimal(3)), ("cancel", episode.episode_id)]
    ports.exit_owned.assert_not_awaited()
    # A cancel acknowledgment is not enough: only the later terminal observation
    # allows closing the filled slice, through its existing actor owner.
    ports.observe.return_value = replace(ports.observe.return_value, entry_terminal=True)
    actor._observations.clear()
    await actor._service_owned(episode.episode_id)
    ports.exit_owned.assert_awaited_once_with(position, "KILLSW1_STOP")
    ports.cancel_entry_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_forced_exit_does_not_need_a_quote_to_reach_owned_exit_port():
    actor, ports, _, position = actor_setup(filled=3, terminal=True)
    ports.stop_requested = lambda: True
    ports.quote = lambda symbol: {}
    await actor.restore_before_serving()
    ports.exit_owned.assert_awaited_once_with(position, "KILLSW1_STOP")


@pytest.mark.asyncio
@pytest.mark.parametrize("proven", [True, False])
async def test_claimed_reservation_only_released_after_durable_unsent_proof(proven):
    actor, ports, episode, _ = actor_setup()
    episode.first_submit_ns, episode.state = None, "claimed"
    ports.stop_requested = lambda: True
    ports.abandon_unsent = AsyncMock(return_value=proven)
    await actor.restore_before_serving()
    ports.abandon_unsent.assert_awaited_once_with(episode.episode_id)
    assert episode.state == ("terminal_no_fill" if proven else "claimed")
    ports.observe.assert_not_awaited()
    ports.cancel_entry_once.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_regular_session_stop_market_close_without_quote(provider, monkeypatch):
    from tests.unit.test_killsw1_momentum_http import real_controlled_sync
    now, broker, service, runtime, identity = await prepared(provider, hour=14, send_exit=False)
    runtime._exit_quote = lambda symbol: {}
    position = (await runtime._observe(identity)).position
    await runtime._exit(position, "KILLSW1_STOP")
    assert exit_orders(broker) == []  # Need a real sync receipt AFTER protection terminal.
    now[0] += timedelta(seconds=1)
    await real_controlled_sync(monkeypatch, now, service, position.account)
    await runtime._exit(position, "KILLSW1_STOP")
    orders = exit_orders(broker)
    assert len(orders) == 1
    assert orders[0].order_type == "market"
    assert orders[0].metadata["session"] == "NORMAL"
    assert "limit_price" not in orders[0].metadata
    assert orders[0].quantity == position.remaining
    assert orders[0].broker_account_name == position.account


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 21])
async def test_extended_session_stale_quote_keeps_native_protection(provider, hour):
    now, broker, _, runtime, identity = await prepared(provider, send_exit=False)
    now[0] = now[0].replace(hour=hour)
    runtime._exit_quote = lambda symbol: {
        "bid": Decimal("3.87"),
        "observed_ns": int((now[0] - timedelta(seconds=3)).timestamp() * 1e9),
    }
    before = list(broker.cancelled)
    await runtime._exit((await runtime._observe(identity)).position, "KILLSW1_STOP")
    assert broker.cancelled == before
    assert exit_orders(broker) == []


@pytest.mark.asyncio
async def test_schwab_pre_seven_stop_keeps_protection_and_sends_nothing():
    now, broker, _, runtime, identity = await prepared("schwab", send_exit=False)
    now[0] = now[0].replace(hour=10)
    before = list(broker.cancelled)
    await runtime._exit((await runtime._observe(identity)).position, "KILLSW1_STOP")
    assert broker.cancelled == before
    assert exit_orders(broker) == []
