"""Bounded real OLOX cap-release adapter; main supplies independently bound state.

Not a candle/OMS replay and never infers missing owner/watch operands from logs.
No CLI, connection, restore, provider call, or synthetic historical defaults.
"""
from hashlib import sha256
from pathlib import Path


SANITIZED_SHA256 = "28811fac407e32236a5b10c13b2e3df17f0fdabfc1b84a770e3ea7f1539bd273"
SELL_MS = 1792602240000
RESUME_BAR_MS = 1792602540000
SELL_OBSERVED_MS = SELL_MS + 62905
RESUME_OBSERVED_MS = RESUME_BAR_MS + 62693
DECISION_ID = f"atr-sell:OLOX:{SELL_MS}"


def replay_olox_same_sell(strategy, *, sanitized_path, load_bound_snapshot, set_clock):
    """Run only after main review in its serial execution slot.

    load_bound_snapshot(label, now_ms) must restore actual historical operands to
    the SAME strategy/state, preserving the new process-local deferred proof across
    the two snapshots. It returns (state, receipt). receipt must identify independently
    hash-bound inputs for watch/boot, flags, bars, owner/inflight, line/gap, and budgets.
    Missing operands must raise, never become empty/flat/true by default. The supplied
    loader and receipts require main's separate review; hashes alone prove no semantics.
    """
    path = Path(sanitized_path)
    if path.name != "SLOTCAP-OLOX-EXPORT-LOGS-SANITIZED-20261021.txt":
        raise ValueError("Only the named SANITIZED input is accepted")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != SANITIZED_SHA256:
        raise ValueError("Sanitized input hash mismatch")
    text = raw.decode("utf-8")
    for marker in (
        f"decision_id={DECISION_ID} bar_time_ms={SELL_MS}",
        "17:10:02,693 INFO project_mai_tai.strategy_core.schwab_1m_v2 | [V2-GAP-RESUME] OLOX",
        f"OLOX segment_id=0 bar_ts={RESUME_BAR_MS}",
    ):
        if marker not in text:
            raise ValueError("Original incident identity/timeline absent")

    receipts = []

    def snapshot(label, now_ms, bar_ms):
        state, receipt = load_bound_snapshot(label, now_ms)
        for key in ("watch_boot", "flags", "bars", "owner_inflight", "line_gap", "budgets"):
            digest = receipt.get(key)
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)):
                raise ValueError(f"UNKNOWN {label} operand binding: {key}")
        if (state.symbol != "OLOX" or not state.bars
                or state.bars[-1].timestamp_ms != bar_ms
                or strategy._bar_observation_phase != "live"):
            raise ValueError("Snapshot is not the original live OLOX boundary")
        receipts.append({"label": label, "bindings": dict(receipt)})
        set_clock(now_ms)
        return state

    state = snapshot("sell", SELL_OBSERVED_MS, SELL_MS)
    if not state.gap_hold_active:
        raise ValueError("Original SELL gap operand missing/false")
    observation = strategy._observe_atr_sell(state, {
        "flip": "SELL", "state": "short", "trail": 1.0926,
        "observation_phase": "live",
    })
    if observation is None or observation.decision_id != DECISION_ID:
        raise ValueError("Original live SELL observation was not admitted")
    proof = state.slotclear_deferred_sell
    state_at_resume = snapshot("resume", RESUME_OBSERVED_MS, RESUME_BAR_MS)
    if state_at_resume is not state or state.slotclear_deferred_sell != proof:
        raise ValueError("Snapshot loader replaced the state or changed deferred proof")
    before = list(strategy.pending_atr_sell_observations())
    queues = (strategy._pending_intents, strategy._pending_webull_direct_intents,
              strategy._pending_webull_fanout_intents)
    queued_before = [list(queue) for queue in queues]
    resumed = strategy._maybe_resume_gap_hold(state)
    after = list(strategy.pending_atr_sell_observations())
    if after != before:
        raise AssertionError("Cap retry changed SELL event identity/queue")
    if [list(queue) for queue in queues] != queued_before:
        raise AssertionError("Cap retry dispatched an entry or cancel")
    return {
        "scope": "bounded cap release only; not full native/candle/OMS replay",
        "input_sha256": SANITIZED_SHA256,
        "decision_id": DECISION_ID,
        "original_sell_ms": SELL_MS,
        "resume_bar_ms": RESUME_BAR_MS,
        "deferred_proof_at_sell": proof,
        "gap_resumed": resumed,
        "slots": [state.cw_resting_taken, state.cw_reclaim_taken],
        "released_sell_ms": state.slotclear_last_fresh_sell_bar_ms,
        "operand_receipts": receipts,
    }
