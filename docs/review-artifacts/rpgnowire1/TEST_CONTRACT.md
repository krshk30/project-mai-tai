# RPGNOWIRE1 test contract

## Frozen source scope

Runtime proof-only release and the shared identity-bearing no-wire refusal-code
validator. No service.py, mirror_retained_hold.py, strategy source, flag or catalog
change. No broker eligibility waiver. No production write, merge or install.

Both exact old opening and any saved replacement require durable pre-submit
intent proof. Entire-generation BrokerOrders, exact-client orders (including
missing generation), contradictory wire metadata, positive Fill rows and fill
latches veto release. Account, strategy, symbol, segment, economic slot, generation
and quantity match; the locked RPG revision must still equal the captured revision.

Feedback is phase=refused, reason=replacement_refused, with
release_reason=old_local_no_wire_return_to_strategy. A saved replacement additionally
has an exact identity-bearing client-audit terminal-zero report. Existing bot
feedback clears only the matching per-account latch; no saved replacement is sent.
The next ordinary v2 draft still runs the unchanged eligibility and sizing paths.

## Own recorded controls

`test_rpgnowire1_proof_release.py` uses the own raw SXTC captures in ASSESSMENT.md.
Its serial release -> real bot feedback -> recorded next completed bar/ask/probe
test asserts exactly one normal draft for the released account and none for its
sibling. A draft is not Schwab acceptance: its cached ineligibility remains.

Archived retired payload, active-type retired payload control, pre-rollback held
payload and controlled stale queued copy are explicitly different cases. Prepared
transfer invalidates the old queue token without resetting the wire budget; a new
ordinary generation promotes through existing admission/upsert. Restart and a
newer v2 latch are separate controls. No held/queued owner's dispatching, uncertain,
filled or different-generation state is waived.

## Legacy expectation adaptations

The developmental full run was 79 failed / 7083 passed against the actual main
baseline 56 failed / 7077 passed. All 23 added failures were older no-wire
clear/price-wait/saved-placement assertions. They are not hidden as baseline.
The following files adapt only that contract; final receipts supersede that run:

| File | Explicit new assertion; preserved control |
| --- | --- |
| test_rpg1_nfq1_composition | Terminal feedback, only normal mirror draft, fresh NFQ dispatch once; old queue cannot dispatch; untouched primary remains blocked |
| test_rpgstuck1 | Recorded VEEA positive proof returns to normal mirror draft; unclassified dispatch stays held |
| test_rpgstuck1_all_on | Four proven-local members of all-14 census return refused with release reason; unknown stays held, SCKT fill stays consumed, zero saved buys after hours; ordinary local draft traverses actual simulated adapter |
| test_rpgstuck1_later_startup | Exact four local releases, no late saved buys, unchanged SCKT 280-share Fill |
| test_rpgstuck1_review_completion | Origin/code proof gates and quiet/evidence-wake loop unchanged; no-wire result is terminal with exact release reason |
| test_rpgstuck1_schwab_sequences | APUS local mirror releases; genuinely wired Schwab cancel/readback/authorization/PLACED chain unchanged |
| test_rpgstuck1_startup | Three exact local releases; incomplete APUS saved-replacement proof still blocks; next normal VEEA/RETO drafts have no RPG token; unknown and positive-fill controls unchanged |
| test_rpgstuck1_loop_liveness | Four no-wire tickets terminate after one loop turn, no saved buy or periodic work; original wired L5 controls remain |

## L7 reachability disclosure

This base has no observed production wired held_unknown -> clear recovery path:
the wired rejected-old/zero proof ends refused. Local recovery previously exercised
that transition; this fix intentionally ends it terminally instead. The new wired
terminal-zero loop control explicitly supplies the transition at the controller
seam, using a genuinely wired fixture and a positive exact-order read. Subsequent
loop ticks, current bot authorization and placed/expired outcomes are real code.
It tests dirty/signal preservation; it is NOT claimed as a recorded production
wired recovery. Removing that dirty/signal block fails both outcomes. No new wired
recovery behaviour is built in this PR.

## Reproduction

Python 3.12: `/Users/velkris/Projects/project-mai-tai/.venv/bin/python`,
`PYTHONPATH=src`, `-p no:cacheprovider`, this worktree's imports only.
Final unit result must compare failed names with
`/tmp/main-1a70-hotfix-rpgnowire-baseline-20261007.xml` (56/7077), including the
volatile momentum throughput failure. No second main run is substituted.

`run_mutations.py` runs isolated in-memory source mutations; source files are
never changed by it. Final reports list every executed mutation, failure names
and any ERROR or survivor. No mutation is called RED without a failing test.
