# EDGEPROT1 lane B: narrow cache local verification
Base: b680c2aff50150ea6130b19ccf0db9e05dfb680d. Owner: B sole writer; integrator queue untouched.
Source: integrator lane assignment and diagnostic instructions; operator-approved FULL card `/Users/velkris/.codex/attachments/05aa46ac-d3ba-45f4-993a-c47cdfb6aac2/Pasted text.txt`.
Assessment preceded edits. Later integrator scope findings and peak diagnostic instructions amend these same IDs; they are not operator rulings.

| ID | Priority | Status | Next action / completion criterion |
| --- | --- | --- | --- |
| EDGEPROT1-E2-B | 2 | Controlled cache GREEN; historical cause BLOCKED | Only committed row-ID/entry-order-ID/entry-price/qty, target-only capture; sell revalidation retained; historical root remains unknown |
| EDGEPROT1-E3-B | 2 | Controlled price/routing verified | Software target floor, no chase, safety cancellation/dedup; next reviewed install only |
| EDGEPROT1-B-PROOF | 2 | Cache load PASS; composition review pending | E1 fixture must use 2.62 and conditional opportunity evidence, not invented broker acceptance |
| EDGEPROT1-B-PEAK | 2 | Four unchanged controls GREEN, reported | 50ms read; peak 2.64 then 2.52: both brokers AM/PM capture before read returns, one resting LIMIT2.62; removed cache capture must remain RED |

Named base wait: `_run_tick_consumer` awaits `_handle_quote_tick_event`, which awaits all managed legs.
Named guard: `_evaluate_v2_managed_exit` rejects quote age >5000ms (gateway produced_at).
Webull submission awaits `_prepare_confirmation_webull_leg`; Schwab awaits `_release_exit_reservation_before_close` when enabled.
New timestamps separate actual decision BEFORE protection from submission. Bound-cache quote intake now stamps a controlled target decision before any await, not retrospectively from a later DB read.
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
Prior prototype proof: baseline new controls27F/1P; preservation537P/1 warning; load13500/60.198050s=224.260/s, stall3.998ms.
Prior mutations: market target18F; nearest floor2F; serial intake2F, restored. Latest-only prototype lost all four transient crosses (4F).
Approved cache focused82P; preservation624P/3 load deselected, existing unawaited attach coroutine plus six SQLite datetime deprecations disclosed.
Final-source cache load13500/60.196111s=224.267/s, max stall13.953ms (<50 unchanged), 1100 background evaluations with injected50ms reads; full suite did not overlap. Earlier cache runs22.624/16.066ms also PASS.
Final E1 be7c86b0 + B applies cleanly in own WT isolated copy:94P/1F/1 load deselected; controlled CORE120 pair2.62/2.29, conditional target fill/no duplicate PASS. Existing E1 fixture2.61 fails: integrator must review wire minimum2.62, modeled proceeds15.60 (not14.40) and recorded-print opportunity evidence together, not blindly weaken expectations. No A WT/source hunk changed.
Rejected: an unbound maximum-bid latch cannot establish freshness/entry threshold/episode. No such mechanism was added.
Approved implementation: immutable four-field cache populated only after a create commits or a mapped managed row loads; boot hydration loads those rows. Flush modifications/deletes and existing falseflip ORM bulk updates invalidate the exact row, including nonbinding price-state changes. Rollback cannot resurrect the previous decision; a later committed load creates a fresh identity fence.
Loads of the writer transaction's own new/modified rows do not publish uncommitted evidence; transaction-end invalidation precedes committed-create publication. Three explicit reload controls RED before this guard, GREEN after. Cold/invalidated rows require a committed load plus a fresh quote, not a retrospectively invented decision.
Quote intake captures one fresh bound target decision before its first await, with actual UTC decision time. No SQL in capture. Existing off-loop snapshot and rare sell-path reads remain ownership/reservation revalidation, not target-trigger discovery; a cold quote worker refuses a DB-derived target until a fresh bound quote arrives.
Capture rejects stale/future quotes, native stand-down and confirmation owners. Consumption checks cache generation and all four fields. Latest stop/floor/flip wins over a retained peak; flip identity is refreshed after the row wait. Native confirmation during the wait suppresses target. Before release and after reservation await, exact row/entry/price/qty are revalidated.
Webull malformed-order correction cannot turn a target into MARKET; Schwab's existing one-shot rejection recovery must retain the four-field target binding. Controlled rejection tests pass; no live broker calls made.
Final-source unchanged HOTFIX1 controls for60s: retained ON239.982/s24.293ms stall/4.663ms handler, OFF239.995/s10.357ms/6.923ms; ON1 then0 DB transactions, OFF0, no queued buys, GC0 both. All three load controls3P in180.99s; no inference about the historical68.446ms failure.
Benchmark coordination: direct notification to main was attempted before the first run, but app rejected ancestor-thread messaging and exposed no native alternative. Commentary disclosed the limitation/windows; main's last snapshot said package untouched, and no other pytest process was present before final B run. No verified delivered message is claimed.
Final mutations: remove bound capture4F; restore MARKET target18F; remove rejection safety/binding guards2F. All restored to GREEN. Four transient controls full AST unchanged, body SHA25646f6274919614fa435ce55ea75f74044b5fbd8c39f5c00b5e12d3ed669fc08b2. HOTFIX1 60s full AST unchanged; A edge body and entire Schwab adapter unchanged againstb680.
Four original transient test bodies/decorators/timestamps/assertions are unchanged; only two second-service all-on/load fixtures now call actual boot hydration. New lifecycle, no-SQL capture, expiry, replacement, safety-during-read and post-reservation change tests are additional, not waivers.
The user supplied reviewer approval for the narrow bound-row cache and resumed B. No production/GitHub/queue writes; B is excluded from tonight's install and considered only for the next reviewed install.
Interim controlled cache/peak, E3/load results and unresolved historical/composition limitations reported. Historical MNDR cause UNKNOWN is not waived by new controls; no recorded counterfactual broker acceptance/fill exists. Local GREEN is not production pin/merge/install approval.
Reproduce combined proof by archiving b680, applying E1 and this commit's diff in an isolated copy, then running combined_replay.py with that copy on PYTHONPATH.
