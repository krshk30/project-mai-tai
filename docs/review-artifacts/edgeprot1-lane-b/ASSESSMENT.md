# EDGEPROT1 lane B: NOT READY
Base: b680c2aff50150ea6130b19ccf0db9e05dfb680d. Owner: B sole writer; integrator queue untouched.
Source: integrator lane assignment and diagnostic instructions; operator-approved FULL card `/Users/velkris/.codex/attachments/05aa46ac-d3ba-45f4-993a-c47cdfb6aac2/Pasted text.txt`.
Assessment preceded edits. Later integrator scope findings and peak diagnostic instructions amend these same IDs; they are not operator rulings.

| ID | Priority | Status | Next action / completion criterion |
| --- | --- | --- | --- |
| EDGEPROT1-E2-B | 2 | BLOCKED / NOT READY | Preserve peak across awaited read with real decision timing, exact episode/freshness and safety precedence; historical root remains unknown |
| EDGEPROT1-E3-B | 2 | Controlled price/routing verified | Software target floor, no chase, safety cancellation/dedup and mutations pass; whole lane remains blocked |
| EDGEPROT1-B-PROOF | 2 | Load PASS; combined fixture RED | E1 fixture must use 2.62 and conditional opportunity evidence, not invented broker acceptance |
| EDGEPROT1-B-PEAK | 2 | Four RED controls, reported | 50ms read; peak 2.64 then 2.52: both brokers AM/PM emit zero orders; do not mark E2 solved |

Named base wait: `_run_tick_consumer` awaits `_handle_quote_tick_event`, which awaits all managed legs.
Named guard: `_evaluate_v2_managed_exit` rejects quote age >5000ms (gateway produced_at).
Webull submission awaits `_prepare_confirmation_webull_leg`; Schwab awaits `_release_exit_reservation_before_close` when enabled.
New timestamps separate actual decision BEFORE protection from submission. They do NOT prove a decision before the first managed-row read.
MNDR final leg ended 13:30:27.341Z after 392.8ms; NO-PAIR 27.050Z, PLACE 27.100Z, old decided_at 27.050Z was AFTER reconciliation.
The 06-27s gap was not a twenty-second wait in that final protection leg. Actual OMS-arrival/guard trace is absent: historical cause UNKNOWN.
SQL archive bid 2.62 event15.643Z received17.456869Z, bid2.64 event16.655Z received17.456878Z. Archive reception is not OMS arrival.
Only four Schwab archive target prints remain (not the card's 721 Massive prints): 100@2.62 at14.575Z, 1000@2.6291 at15.562Z, then2.6499/2.6415.
Recorded LNKS: buy196@1.53 11:02:55.219Z, LIMIT1.60 sell@1.61 11:03:22.099Z; controlled new target floor1.61.
Recorded MNDR: buy120@2.49 13:06:47.644Z, MARKET sell@2.3113 13:30:27.357Z; controlled new floor2.62.
Recorded CLWT: buy133@2.24 13:35:10.841Z, OCO sell133@2.03 13:44:02.377Z; controlled attribution creates no second software sell.
Captured actual OMS PID2431239 flags are sanitized in live-flags.env; no new flag. +5/-8, floor/rearm OFF retained.

Scope: software CW_TARGET ceiling for both brokers/all sessions and Webull exit-only pair (E1/existing RTH attach).
Schwab adapter, Schwab exit_only_oco edge/native-entry rounding, Webull master-combo and realignment retain original source/tests.
The integrator's scope clarification removed the broader native-rounding proposal. No stop-type, buy-drift, ORB/shared-account or A worktree change.
E3 safety fix: target is a durable resting order, not an EH sticky retry decision that could replace a newer stop/floor/flip.
Existing EH test now asserts durable working target/floor and no blind target retry; flip retry remains tested. No waiver/xfail.
Proof: baseline new controls27F/1P; scoped preservation537P/1 warning; final load13500/60.198050s=224.260/s, stall3.998ms (<50 unchanged).
Mutations: market target18F; nearest floor2F; serial intake2F. Restored. Peak controls intentionally stay RED.
E1 be7c86b0 + B applies cleanly: controlled CORE120 pair2.62/2.29, conditional target fill/no duplicate PASS; existing E1 fixture2.61 fails.
Candidate latching NOT implemented: cold read lacks a bound entry threshold; a maximum-bid latch alone cannot establish freshness/episode/safety priority.
Source assessment: `_managed_v2_symbols` and `_rehydrate_managed_v2_symbols` retain only account/symbol; `_V2ManagedSnapshot` obtains row ID/entry after the await.
Assessed plan, awaiting human design review requested by main: publish an immutable row-ID/entry cache after committed managed writes, at boot and on poll re-enrollment; invalidate replaced/closed episodes.
Then capture one fresh target decision in quote intake before any await, bound to that row; before release/submission validate the same row and prioritize latest hard-stop/floor/flip.
Retain existing dedup/cancellation and target ceiling. Add replacement/rollback, stale/future quote, safety-during-read and concurrent-quote controls; keep the timestamp assertion.
Alternative bounded quote evidence preserves an observation, not a cold-read target decision; it cannot justify backdating `cw_decided_at`. No cache/latch built.
Latest integrator diagnostic instruction preserves the RED control and NOT READY build; no deeper mechanism until the human design-review reply. Integrator owns other work/queue.
Interim assessment, E3/load results and all historical/peak/composition blockers reported. This local candidate is not pin/merge/install-ready.
Reproduce combined proof by archiving b680, applying E1 and this commit's diff in an isolated copy, then running combined_replay.py with that copy on PYTHONPATH.
