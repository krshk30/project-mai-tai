"""Actual producer/barrier methods, controlled canonical RECLAIM route; no historical BIYA claim."""
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, TradeIntent
from project_mai_tai.v2_removed_wait import RemovedWait
from tests.unit import test_nfq2_eh_fresh_price as fixtures
from tests.unit.test_v2_flip_owned_first_entry import _strategy


@pytest.fixture
def service(monkeypatch):
    return fixtures.service.__wrapped__(monkeypatch)


def protocol(service, *, webull=False):
    event = fixtures.biya(reactive=True, webull=webull)
    strategy, _, _, _ = _strategy(dual=webull)
    state = strategy.watchlist_state("BIYA")
    event.payload.metadata.update(strategy._fanout_identity_metadata(state, source="reactive",
        segment_id=fixtures.SEGMENT))
    assert event.payload.metadata["fanout_slot"] == "reclaim"
    held = fixtures.hold(service, event)
    request = RemovedWait("BIYA", fixtures.SEGMENT, str(uuid4()), int(fixtures.NOW.timestamp() * 1000),
                          (event.payload.broker_account_name,))
    strategy._queue_removed_wait_barriers(state, request)
    draft = (strategy._pending_webull_direct_intents if webull else strategy._pending_intents)[-1]
    barrier = event.model_copy(deep=True)
    barrier.event_id = uuid4()
    barrier.payload.intent_type, barrier.payload.side = draft.intent_type, draft.side
    barrier.payload.metadata = dict(draft.metadata)
    assert service._nfq2_bound_identity(barrier) and service._nfq2_applies(event)
    return held, barrier


@pytest.mark.asyncio
@pytest.mark.parametrize("webull", [False, True])
@pytest.mark.parametrize("phase", ["held", "queued"])
async def test_typed_opportunity_barrier_retires_local_reclaim_and_invalidates_queue(service, phase, webull):
    held, barrier = protocol(service, webull=webull)
    old = None
    if phase == "queued":
        fixtures.quote(service)
        await service._evaluate_nfq2_holds("BIYA")
        old = fixtures.retry(service)
    service._nfq2_observe(barrier)
    assert held.phase == "retired" and not service._nfq2_holds
    if old:
        assert not service._claim_nfq2_retry(old)
    with service.session_factory() as session:
        row = session.get(DashboardSnapshot, held.row_id)
        assert row.payload["phase"] == "retired" and row.payload["token"] == ""
        assert session.get(TradeIntent, held.intent_id).status == "rejected"
        assert session.scalars(select(BrokerOrder)).all() == []
    count = len(service.redis.entries)
    service.__dict__["_eh_price_holds"] = {}
    service._restore_nfq2_holds()
    fixtures.quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    assert not service._nfq2_holds and len(service.redis.entries) == count


@pytest.mark.parametrize("change", ["untyped", "opportunity", "token", "side", "account", "symbol", "strategy",
    "generation", "targeted_cancel"])
def test_invalid_or_other_scope_barrier_does_not_retire_reclaim(service, change):
    held, barrier = protocol(service)
    md = barrier.payload.metadata
    if change == "untyped":
        md.pop("clearwait_buy_only")
    elif change == "opportunity":
        md["clearwait_opportunity_id"] = "another"
    elif change == "token":
        md["clearwait_removal_token"] = "bad"
    elif change == "side":
        barrier.payload.side = "sell"
    elif change == "account":
        barrier.payload.broker_account_name = "other"
    elif change == "symbol":
        barrier.payload.symbol = "OTHER"
    elif change == "strategy":
        barrier.payload.strategy_code = "orb_schwab"
    elif change == "generation":
        md["nfq2_generation"] = "another"
    else:
        # Ordinary exact RESTING cancels are not opportunity-wide.
        md.pop("clearwait_opportunity_id")
        md["target_client_order_id"] = "another-resting-order"
    service._nfq2_observe(barrier)
    assert held.phase == "held" and service._nfq2_holds[service._nfq2_key(held.event)] is held


@pytest.mark.parametrize("phase", ["dispatching", "uncertain"])
def test_typed_barrier_never_retires_dispatch_or_uncertainty(service, phase):
    held, barrier = protocol(service)
    held.phase, held.token = phase, "controlled-dispatch-generation"
    with service.session_factory() as session:
        service._nfq2_save(session, held)
        session.commit()
    service._nfq2_observe(barrier)
    assert held.phase == phase and held.token == "controlled-dispatch-generation"
    with service.session_factory() as session:
        assert session.get(DashboardSnapshot, held.row_id).payload["phase"] == phase


@pytest.mark.parametrize("status", ["accepted", "cancelled", "filled", "aborted"])
def test_any_related_order_is_not_proven_local_even_with_terminal_label(service, status):
    held, barrier = protocol(service)
    with service.session_factory() as session:
        intent = session.get(TradeIntent, held.intent_id)
        order = BrokerOrder(intent_id=intent.id, strategy_id=intent.strategy_id,
            broker_account_id=intent.broker_account_id, client_order_id="controlled-related-wire",
            broker_order_id="controlled-broker", symbol="BIYA", side="buy", order_type="limit",
            time_in_force="day", quantity=intent.quantity, status=status, payload=deepcopy(held.event.payload.metadata))
        session.add(order)
        session.commit()
        oid = order.id
    service._nfq2_observe(barrier)
    assert held.phase == "held"
    with service.session_factory() as session:
        assert session.get(BrokerOrder, oid).status == status
        assert session.get(TradeIntent, held.intent_id).status == "held"


@pytest.mark.parametrize("change", ["intent_status", "intent_quantity", "durable_dispatch"])
def test_pre_wire_proof_is_required_for_segment_retirement(service, change):
    held, barrier = protocol(service)
    with service.session_factory() as session:
        intent = session.get(TradeIntent, held.intent_id)
        if change == "intent_status":
            intent.status = "submitted"
        elif change == "intent_quantity":
            intent.quantity += 1
        else:
            row = session.get(DashboardSnapshot, held.row_id)
            row.payload = {**row.payload, "dispatch": {"generation": "unknown-wire"}}
        session.commit()
    service._nfq2_observe(barrier)
    assert held.phase == "held"


@pytest.mark.asyncio
async def test_barrier_rereads_durable_queued_shadow_and_prevents_restart_resend(service):
    held, barrier = protocol(service)
    before = service._nfq2_copy(held)
    queued = service._nfq2_copy(held, phase="queued", token="worker-queued-shadow")
    with service.session_factory() as session:
        assert service._nfq2_transition(session, before, queued)
        session.commit()
    assert held.phase == "held"  # Controlled off-loop commit before event-loop projection.
    service._nfq2_observe(barrier)
    assert held.phase == "retired" and not service._nfq2_holds
    with service.session_factory() as session:
        assert session.get(DashboardSnapshot, held.row_id).payload["phase"] == "retired"
    service._restore_nfq2_holds()
    fixtures.quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    assert not service.redis.entries


def test_barrier_cas_mismatch_never_erases_won_dispatch(service, monkeypatch):
    held, barrier = protocol(service)
    original = service._nfq2_transition

    def race(session, before, after, **kwargs):
        row = session.get(DashboardSnapshot, held.row_id)
        row.payload = {**row.payload, "phase": "dispatching", "dispatch": {"generation": "won-dispatch"}}
        session.flush()
        return original(session, before, after, **kwargs)

    monkeypatch.setattr(service, "_nfq2_transition", race)
    service._nfq2_observe(barrier)
    assert held.phase == "held" and service._nfq2_holds
    with service.session_factory() as session:
        p = session.get(DashboardSnapshot, held.row_id).payload
        assert p["phase"] == "dispatching" and p["dispatch"]["generation"] == "won-dispatch"
