"""Canonical RECLAIM routing control, NOT historical BIYA bars or dispatch evidence."""
from uuid import uuid4

import pytest

from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.v2_removed_wait import RemovedWait
from tests.unit import test_nfq2_eh_fresh_price as fixtures
from tests.unit.test_v2_flip_owned_first_entry import _strategy


@pytest.fixture
def service(monkeypatch):
    return fixtures.service.__wrapped__(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("webull", [False, True])
@pytest.mark.parametrize("phase", ["held", "queued"])
async def test_typed_segment_barrier_retires_proven_local_reclaim_hold(service, phase, webull):
    event = fixtures.biya(reactive=True, webull=webull)
    # Recorded helper actually uses RESTING. Only this labelled protocol routing tuple is changed.
    event.payload.metadata.update(fanout_slot="reclaim", fanout_slot_id=fanout_slot_id(
        strategy_code="schwab_1m_v2", symbol="BIYA", segment_id=fixtures.SEGMENT, slot="reclaim"))
    assert service._nfq2_applies(event) and service._nfq2_bound_identity(event)
    held = fixtures.hold(service, event)
    queued = None
    if phase == "queued":
        fixtures.quote(service)
        await service._evaluate_nfq2_holds("BIYA")
        queued = fixtures.retry(service)
    strategy, _, _, _ = _strategy(dual=webull)
    state = strategy.watchlist_state("BIYA")
    request = RemovedWait("BIYA", fixtures.SEGMENT, str(uuid4()), int(fixtures.NOW.timestamp() * 1000),
                          (event.payload.broker_account_name,))
    strategy._queue_removed_wait_barriers(state, request)
    draft = (strategy._pending_webull_direct_intents if webull else strategy._pending_intents)[-1]
    barrier = event.model_copy(deep=True)
    barrier.event_id = uuid4()
    barrier.payload.intent_type, barrier.payload.side = draft.intent_type, draft.side
    barrier.payload.metadata = dict(draft.metadata)
    assert service._nfq2_bound_identity(barrier)
    assert barrier.payload.metadata["fanout_slot"] == "resting"
    assert barrier.payload.metadata["clearwait_buy_only"] == "true"
    assert barrier.payload.metadata["clearwait_opportunity_id"] == str(fixtures.SEGMENT)
    service._nfq2_observe(barrier)
    print("PROTOCOL_CANCEL_CONTROL", {"historical_reclaim_claim": False, "webull": webull,
        "before": phase, "after": held.phase, "hold_slot": "reclaim", "barrier_slot": "resting"})
    assert held.phase == "retired" and service._nfq2_key(event) not in service._nfq2_holds
    if queued is not None:
        assert not service._claim_nfq2_retry(queued)
