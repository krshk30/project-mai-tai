"""Execute labelled controlled episode replays; never claim counterfactual fills."""
from __future__ import annotations

import json

from tests.unit.test_falseflip1_runtime import ack, book, close_and_cancel, runtime
from tests.unit.test_falseflip1_step0 import CASES, FALSE_SLOTS, REAL_CASES
from tests.unit.test_v2_retry_one import _bar


def main():
    results = []
    for slot in FALSE_SLOTS:
        rows = [row for row in CASES if row["slot"] == slot]
        sessions = []
        for pm in (False, True):
            strategy, state, clock, proofs, _ = runtime(rows, pm=pm)
            closed_check_ms = clock[0]
            close_and_cancel(strategy, state, clock, proofs)
            clock[0] += 60000
            state.bars.append(_bar(clock[0]))
            book(strategy, state, clock, proofs)
            strategy._falseflip_restoring_track(state)
            primary = strategy.drain_pending_intents()
            mirror = strategy.drain_webull_direct_intents()
            assert state.resting_active and state.atr_state == "short"
            assert len(strategy._falseflip_budget(state).episodes) == 1
            assert strategy._falseflip_effective_closes(state, 1) == 0
            assert (not primary and not mirror) if pm else len(primary) == len(mirror) == 1
            assert not state.resting_is_broker_order if pm else state.resting_is_broker_order
            ack(strategy)
            sessions.append({"session_control": "PM software" if pm else "RTH broker",
                "verdict": "PASS", "restored": True, "scalar_state": state.atr_state,
                "scalar_trail": state.atr_trail, "resting_trigger": state.resting_trigger,
                "close_proof_check_ms": closed_check_ms, "restored_at_ms": clock[0],
                "controlled_elapsed_ms": clock[0]-closed_check_ms,
                "primary_open_drafts": len(primary), "mirror_open_drafts": len(mirror),
                "soft_wait_armed": pm, "broker_fill_outcome": "UNMEASURED: emitted drafts are not broker fills",
                "functions": ["apply_flip_position_book", "pending_falseflip_budgets",
                    "acknowledge_falseflip_budget", "acknowledge_falseflip_cancel_publication",
                    "apply_removed_wait_proofs", "_falseflip_restoring_track", "_queue_resting_place"]})
        bindings = []
        for row, proof in zip(rows, proofs, strict=True):
            original = (row["managed_entry_order_id"] == row["order_id"]
                        and row["managed_entry_client_order_id"] == row["fill_client_order_id"]
                        and row["managed_account"] == row["account"] and row["managed_symbol"] == row["symbol"])
            bindings.append({"fill_id": row["fill_id"], "account": row["account"],
                "original_binding_proven": original, "controlled_binding": not original,
                "managed_row_id": proof.identity.managed_row_id,
                "entry_order_id": proof.identity.entry_order_id,
                "entry_client_order_id": proof.identity.entry_client_order_id})
        results.append({"symbol": rows[0]["symbol"], "slot": slot, "binding_controls": bindings,
            "scope": "Recorded entry scalars; controlled owner/cancel/next-bar admission, not full historical broker replay",
            "historical_restore": "UNMEASURED: feature not installed", "sessions": sessions})
    real = []
    for row in REAL_CASES:
        strategy, state, clock, proofs, _ = runtime([row])
        book(strategy, state, clock, proofs)
        strategy.drain_pending_intents()
        strategy.drain_webull_direct_intents()
        clock[0] += 60000
        state.bars.append(_bar(clock[0]))
        strategy._queue_resting_place(state, state.atr_trail, slot="first")
        assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
        assert state.flip_owner_phase == "consumed" and not strategy.pending_falseflip_budgets()
        real.append({"fill_id": row["fill_id"], "symbol": row["symbol"], "filled_at": row["filled_at"],
                     "classification": proofs[0].kind, "verdict": "PASS", "second_open_drafts": 0})
    print(json.dumps({"false_slots": len(results), "session_controls": 2*len(results),
                      "cases": results, "real_controls": real}, indent=2))


if __name__ == "__main__":
    main()
