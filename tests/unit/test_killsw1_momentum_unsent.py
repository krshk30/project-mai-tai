from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from project_mai_tai.db.models import BrokerOrder, TradeIntent
from project_mai_tai.momentum_live.admission import Refusal
from project_mai_tai.oms.bot_control import BotControlStore
from tests.unit.test_momlive1_persistence import setup


def provision(factory, episode, *, stopped):
    with factory() as session:
        parent = session.get(TradeIntent, UUID(episode.episode_id))
        strategy, account = parent.strategy_id, parent.broker_account_id
    controls = BotControlStore(factory)
    controls.seed(strategy)
    if stopped:
        controls.request(strategy_id=strategy, command_id=uuid4(), request_id=uuid4(),
            expected_generation=0, action="STOP", account_ids=(account,),
            actor_reference="operator", reason="test claimed no-wire cleanup")
    return strategy, account


@pytest.mark.parametrize("stopped", [False, True])
def test_claim_without_any_order_requires_actual_durable_stop(stopped):
    factory, store, _, episode, _ = setup()
    provision(factory, episode, stopped=stopped)
    with factory.begin() as session:
        assert store.abandon_unsent_entry(session, episode.episode_id) is stopped
    with factory() as session:
        parent = session.get(TradeIntent, UUID(episode.episode_id))
        assert parent.payload["momlive_episode"]["state"] == ("terminal_no_fill" if stopped else "claimed")


@pytest.mark.parametrize("marker", [None, "{}", "uncertain", False])
def test_even_corrupt_or_false_permit_key_never_means_no_wire(marker):
    factory, store, _, episode, request = setup()
    provision(factory, episode, stopped=True)
    request = replace(request, metadata={**request.metadata, "bot_control_generation": "0"})
    with factory.begin() as session:
        store.prepare_order(session, episode.episode_id, request, provider="schwab")
        order = session.query(BrokerOrder).filter_by(client_order_id=request.client_order_id).one()
        order.payload = {**order.payload, "bot_control_permit": marker}
    with factory.begin() as session:
        with pytest.raises(Refusal, match="dispatch_unknown"):
            store.abandon_unsent_entry(session, episode.episode_id)
    with factory() as session:
        parent = session.get(TradeIntent, UUID(episode.episode_id))
        assert parent.payload["momlive_episode"]["state"] == "claimed"


def test_owned_pending_controlled_order_without_permit_is_proven_unsent_not_filled():
    factory, store, _, episode, request = setup()
    provision(factory, episode, stopped=True)
    request = replace(request, metadata={**request.metadata, "bot_control_generation": "0"})
    with factory.begin() as session:
        store.prepare_order(session, episode.episode_id, request, provider="schwab")
    with factory.begin() as session:
        assert store.abandon_unsent_entry(session, episode.episode_id)
    with factory() as session:
        order = session.query(BrokerOrder).filter_by(client_order_id=request.client_order_id).one()
        assert order.status == "aborted" and order.broker_order_id is None
        assert order.payload["momlive_entry_never_dispatched"] is True
