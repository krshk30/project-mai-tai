"""[codex] Review-only RED: controlled accepted delta, not a historical working-order claim."""
import copy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import Base, BrokerOrder
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.v2_removed_wait import RemovedWaitStore
from tests.unit.test_removed_wait_session_expiry import RAW, OLD, WEBULL, PRIMARY, _case


@pytest.mark.asyncio
@pytest.mark.parametrize("date_guard", [False, True])
async def test_known_prior_working_buy_retains_exact_canonical_cancel(tmp_path, monkeypatch, date_guard):
    strategy, state, _, _, request = _case(episode=OLD)
    engine = create_engine("sqlite:///" + str(tmp_path / "controlled.db"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    store, oms = RemovedWaitStore(sessions), OmsStore()
    # Retained OLOX identity/qty; accepted status is a CONTROLLED safety counterexample.
    raw = RAW["orders"][0]
    with sessions() as session:
        owner = oms.ensure_strategy(session, "schwab_1m_v2")
        accounts = {name: oms.ensure_broker_account(session, name, provider="webull" if name == WEBULL else "schwab",
                                                   environment="live").id for name in (PRIMARY, WEBULL)}
        old_order = BrokerOrder(strategy_id=owner.id, broker_account_id=accounts[WEBULL],
            symbol="OLOX", side="buy", client_order_id=raw["client_order_id"],
            broker_order_id=raw["broker_order_id"], order_type="STOP_LIMIT", time_in_force="day",
            quantity=raw["quantity"], status="accepted", payload=copy.deepcopy(raw["payload"]))
        session.add(old_order)
        session.commit()
        # The existing canonical OMS selector positively selects the old episode, not symbol alone.
        selected = oms.find_open_order_for_cancel(session, strategy_id=owner.id,
            broker_account_id=accounts[WEBULL], symbol="OLOX",
            metadata={"clearwait_buy_only": "true", "fanout_segment_id": str(OLD)})
        assert selected is old_order
    store.record(request, True)
    if not date_guard:
        # Explicit guard-off counterfactual control; not an actual main checkout run.
        monkeypatch.setattr(strategy, "_removed_wait_episode_is_prior", lambda _: False)
    strategy.configure_removed_wait(store.record, restored=store.restore(), readable=True)
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strategy, strategy.settings, store
    before = copy.deepcopy(state)
    proof = store.proofs((request,), {PRIMARY, WEBULL})[0]
    assert not proof.clear
    await bot._removed_wait_poll()
    assert strategy._removed_wait_requests == {"OLOX": request} and state == before
    drafts = strategy._pending_intents + strategy._pending_webull_direct_intents
    assert any(d.intent_type == "cancel" and d.metadata.get("fanout_segment_id") == str(OLD)
               for d in drafts), "Known exact old working BUY has no canonical cancellation after date suppression"
