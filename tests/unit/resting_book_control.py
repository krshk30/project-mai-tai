"""Controlled primary ownership proof, never historical broker evidence."""

from project_mai_tai.fanout_identity import fanout_slot_id


def prove_primary_rest(strategy, state):
    now = strategy._now_ms()
    state.fanout_segment_id = state.fanout_segment_id or now
    state.resting_schwab_generation = state.resting_schwab_generation or "CONTROLLED-GENERATION"
    state.last_resting_placed_slot = state.last_resting_placed_slot or "first"
    state.resting_is_broker_order = True
    strategy.apply_primary_resting_book(observed_at_ms=now, orders={
        (state.symbol, state.resting_schwab_generation): {
            "status": "working", "broker_order_id": "CONTROLLED-PRIMARY-ORDER",
            "segment_id": state.fanout_segment_id, "slot": "resting",
            "slot_id": fanout_slot_id(strategy_code="schwab_1m_v2", symbol=state.symbol,
                segment_id=state.fanout_segment_id, slot="resting"),
            "entry_slot": state.last_resting_placed_slot, "submit_response_at_ms": 0,
        },
    })
    assert strategy._primary_resting_proven(state)
