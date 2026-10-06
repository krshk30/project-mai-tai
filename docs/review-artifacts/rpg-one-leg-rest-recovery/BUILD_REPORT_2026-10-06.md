# T43: one-leg recovery and fresh pre-wire authorization

Current local status is the final proof-only receipt below. Earlier readiness
estimates, marker policy and test counts are historical, not current proof.

Independent cause verdict: AGREE with the corrected local-abort cause. Own
11:05 ET count is 2/78 token-bearing OPENs. Exact authorization-to-final-check
p99 is UNMEASURED: the authorization field is mutable and completed_at is not
the check timestamp. The fix requests a new current v2 decision instead of
widening the one-second authorization guard. No production action.

## Behaviour and safety

Only a refused replacement with positively cleared old order, no replacement
fill, matching account/slot/segment/generation and a later completed bar reaches
ordinary placement. The producer is scoped to the absent account; the surviving
leg's generation, quantity and wire prices remain unchanged. Existing ownership,
window, price, liquidity, sizing, fill and uncertain-dispatch gates still run.

On a stale check, OMS releases its transaction, requests a nonce, and waits for
v2's real current-gate evaluation using the existing two-second read budget.
After acknowledgement it reacquires the shared-account advisory lock and repeats
ORB collision and risk admission as well as the existing canonical-price,
quantity, slot, segment, dispatch-token, freshness and fill checks. No clock
stamp is forged. A changed price or an unreadable acknowledgement aborts.

Local RPG refusals now store order and intent status=aborted, the client-origin
code, an aborted audit event and [OMS-RPG1-ABORT]. Real venue refusals remain
rejected with broker-origin reason. No historical rows or ledger are edited.
Crash recovery accepts an aborted replacement only with its exact bound identity,
zero fill, no broker id, matching client-origin intent and client audit, and
positively cleared old order. Unproven rows remain submit_unknown.
The same read-only proof is used by v2's in-flight-position and unknown-owner
readers and by NFQ retirement. An unproven aborted row remains blocking; it is
not treated as a flat or cleared broker order. Companion pending-latch readers
understand the new local terminal status. Broker cancel-proof rules are unchanged.
An ordinary OPEN refused because an old ticket still owns the buy can also be
audited aborted: exact event id, affirmative no-wire marker and matching client
audit terminalize only that NEW intent. They never clear the separately owned
old ticket. Missing event id or non-affirmative marker remains in-flight/blocked.
Interrupted broker-rejection completion is separately repairable only with a
positive broker audit, exact replacement identity and intent, no fill and old
order proven clear. Cancel/expiry or status-only evidence cannot retry a leg.

## Recorded acceptance and disclosed controls

| Case | Recorded inputs | Assertion / limitation |
|---|---|---|
| OLOX Schwab10:15 ->10:16 | Ticket c9b13cc4; auth age0.822015 at submit,1.085075 at completion; trail1.456585, bar14:14Z, capture bid1.36/ask1.37 | Named timing replay obtains a new nonce and retains identity; ordinary later-bar replay emits primary only. Later acceptance is simulated, not proof of a fill or profit. |
| AIXI Webull09:45 -> next bar | Ticket a5b034cb; auth age0.913296 at submit,1.340543 at completion; trail2.944435, bar13:44Z, capture bid2.50/ask2.53 | Named timing replay obtains a new nonce with unchanged identity; later-bar replay emits mirror only. Capture ask2.53 is outside the unchanged Webull distance rule versus stop2.9592, so live placement/acceptance is NOT claimed. |
| Interrupted committed client abort | Existing real runtime submit path, controlled interruption after its durable abort audit | Exact audited no-wire replacement releases; missing audit, wrong audit source/token, broker id or unknown origin stays blocked. |
| Interrupted broker rejection | Real OMS submit/report path, simulated venue refusal, controlled interrupted coordinator completion | Positive bound broker audit supplies completion time; next bar emits failed leg once, sibling unchanged. No audit, client audit, foreign generation, Fill, cancel or expiry emits no repair. |
| Concurrent ORB admission during nonce wait | Explicit controlled competing accepted ORB order, same account/symbol | v2 aborts, no second broker open; the ORB order remains accepted and unchanged. |
| All-on / flip owner | Eight candidate switches true plus controlled restored owner/position evidence | Either absent account may be repaired once; sibling remains unchanged, no bypass of first-slot ownership. |

The named race test uses the six-decimal retained ATR probe, not the four-decimal
metadata line. Initial harness failures exposed zero cached notional, millisecond
decision-clock precision and the rounded AIXI trail; each fixture was corrected
to the stated recorded/live values without relaxing production comparisons.

RPG broker hand-offs are RTH-only. A pre-market software reprice does not submit
a broker replacement and therefore cannot create this replacement-refused ticket.
Installed pre-market PMREST/PMPRINT/PMFLIP tests and the all-on composition remain
required regressions; no extra pre-market entry path or reclaim switch is added.
The next-bar sibling/quote inputs and future venue responses are controlled;
capture quotes do not attest what was in the OMS decision cache.

## Verification

As of 2026-10-06 11:45 ET, target review-ready:2026-10-06 15:00 ET. Not ready until
final suite and CI receipts. Early focused runtime/composition:272 PASS;
expanded PM/NFQ/fanout set545 PASS before the last abort-reader proof refinement.
New tests43 PASS in28.62s, including named races, both legs, all-on/flip owner,
positive exact abort recovery, ordinary-abort negative proofs and14 broker-crash
controls. The preceding frozen full run (before broker-crash follow-up) had
47 FAIL/6444 PASS, identical failed names to main; focused953 PASS. The final
post-follow-up full and focused suites and20-mutation run are running; these
earlier receipts are NOT claimed as the final head's full suite.
The preceding16-mutation run had15 assertion-RED; removing the order-origin
check alone survived because the linked intent and matching audit independently
enforce it (not counted RED). Refreshed targets cover:
missing-leg branch, sibling scope, generation, refresh request, age guard,
canonical price, order/intent abort classification, post-wait ORB collision,
abort crash recovery, client audit source, generation, broker id, origin and
in-flight reader, affirmative ordinary no-wire/event id and broker-crash audit
classification/identity. Initial
mutation target/None-audit errors were NOT counted RED; corrected targets require
AssertionError, not an import/compile exception.

Full comparison baseline: exact main3ebde364, independent fresh unit run
/tmp/orbpurple1-unit-base.log:47 FAIL /6416 PASS; raw failed names will be compared
with the final T43 run, not with an assumed 56-failure historical baseline.
Linux CI remains authoritative for its own environment; both Validate runs must
be green before ready. PostgreSQL advisory-lock operation is UNEXERCISED locally
(SQLite race simulation only); no live broker dispatch or live latency claim.
Initial draft053a7179 CI failed only the old terminal-intent set assertion,
which expected three statuses; follow-up pins the four-status set including
audited aborted. No failing assertion is dropped. Independent read-only audit
reproduced the broker-crash omission before this fix and found no remaining
material safety finding in the final source review. It is not a review pin.

## Own raw paths

| Receipt | SHA256 |
|---|---|
| /tmp/codex-t43-latest-named-quotes-20261006.jsonl | fbccea847e35d93cc99e4615dd195e29e233b89eb538317b679767544ea7ce5e |
| /tmp/codex-t43-named-bars-20261006.jsonl | 6c83499fb2138e8768797a0b401e90935cae7ffa4bec476142aa217eb2ece0b9 |
| /tmp/codex-t43-aixi-probe-20261006.txt | b794e859f7612895dc1e9527d355ede92115c24ecf874a93bf050c56308b9747 |
| /tmp/codex-t43-olox-probe-20261006.txt | 801ab6422c21210e70170c395d928a9521cdaeab2af1992212551beec97c415a |

Queries were BEGIN READ ONLY, timeout5s, two names, one last available row each;
no snapshot-batches read. Initial global LIMIT50 quote query omitted OLOX; the
per-name bounded lateral query corrected that, not treated as an empty market.

Restart list if pinned: OMS + v2 (coordinated with strategy companion in the
separately reviewed install plan). No migration, gateway/ORB/paper change or new
setting. The existing hand-off switch remains the rollback for new hand-offs;
reverting this source requires its own exact-SHA approval, not an automatic
rollback. Tonight's three-item set is unchanged until a separate pin and plan
binding explicitly include this PR.

## OLOX cancellation-chain follow-up (pre-full-head checkpoint)

NOT READY. This section supersedes the earlier final-source-review statement:
the resumed review found a stale placed-generation suppression and a separate
inactive/different-generation unproven-ownership bypass. Both are covered by the
current patch; full final-head and new-head CI proof are still pending.

The actual 10:13:04 Schwab placed report binds token
48c765e5-a03a-5fe7-b488-78355518da16 and replacement generation
48c765e5-a03a-5fe7-b488-78355518da16:0. The actual c9b13cc4 successor has recorded
old-order clearance; a claim that c9 lacks that clearance is not supported.
Historical placed cache retention, surviving sibling phase, renewed quote and
continuation state are controlled reconstruction, NOT recovered live cache.
Actual live-cache causation remains UNMEASURED.

Baseline aaac903e methods loaded process-locally on identical reconstructed
inputs give primary0/mirror0 at 10:16:03, active=true, primary quantity0,
entry_owned=false, and equal replacement/state generations. Changing only the
state's primary generation to an empty string gives primary1/mirror0. The
counter-control captures quantity before and after queueing separately (0/408);
408 is not the pre-queue quantity. Parent independently obtained the same result.
Its raw /tmp/codex-t43-parent-generation-countercontrol.log SHA256 is
c7ca71a7ef9aca97cc1b7a7f4525daeaa15566098d678319198390c5f0210913.
Own reproducible local historical-method script is check_generation_countercontrol.py;
own raw /tmp/codex-t43-generation-countercontrol-20261006.log SHA256 is
d47f0fdb37184752d11a4dc184d691c0db83322a5e08e2b4537841ddb29edbf1.
This script requires historical Git objects; current safety tests are independent
of Git history and run in the shallow CI checkout.

Cancellation retirement now requires the exact successor original-order UUID,
account, strategy, symbol, BUY/client/broker identity, finite quantity, generation,
segment and slot; recorded terminal CANCELED/CANCELLED zero clearance, no no_rebuy,
and no linked Fill. Status/age/DB absence alone never retires a replacement.
The journal CAS publishes a terminal-proof marker. Feedback clears only the
matching owned generation and retains its retired identity for scoped later-bar
recovery, without resetting consumed slots or touching a sibling's prices/size.
The real recorded chain plus controlled continuation emits one primary draft,
zero mirror drafts and one simulated ordinary OMS open. No live broker action.

A still-placed replacement owns buy admission even if shared active=false or
the projected generation is empty/foreign. Healthy placed legs remain eligible
for serial cancel/read; this is intentionally distinct from buy ownership.
Filled ownership is scoped to its consumed slot, not an independent reclaim.
Unproven terminal-looking projections cannot clear generation or quantity.
The old cancelled-restart fixture now includes durable successor-zero proof and
an ended opportunity; its real authorization expires that successor and retains
the original one-open expectation. The all14 positive placed expectation is
phase-specific; original exact wire counts, unknown controls and after-hours
zero-open assertions remain. The later SCKT census has two historical status-only
terminal projections without successor proof: they remain owned even though a
separate recorded Fill consumes the first slot. No historical proof is invented.

Only the requested aborted display/acceptance status sites were added. No
MIRRORHOLD expansion, distance/nonce/authorization-age relaxation, production
read/write/action, ledger repair, migration, merge or deployment is part of this
follow-up. Unknown ownership can remain held until exact proof becomes available;
timeout or a new generation is not a liveness escape hatch.

Frozen expanded focus:1077 PASS in84.21s; raw
/tmp/codex-t43-safe-chain-focus-bound-final-20261006.log. Additional startup
compatibility:152 PASS in35.98s after preserving the SCKT unknown contract;
raw /tmp/codex-t43-safe-chain-startup-focus-final-20261006.log.
Mutations:35/35 assertion-RED; raw
/tmp/codex-t43-safe-chain-mutations-bound-final-20261006.json. Earlier 33/35 and
import/misbinding failures are NOT counted as proof. The acceptance mutant binds
the collected test's actual file-loaded reader. The original origin mutant was
masked by audit-metadata matching; the refined controlled order/audit corruption
isolates the order-origin predicate while preserving the linked client intent.
Unmutated controls:9 PASS. Ruff src/tests and diff check pass.

Final pair baseline is exact main c21d8274fcd1d3129d61207a33dd7b002a7c9e8c:
47 FAIL/6447 PASS in291.24s, raw
/tmp/codex-t43-safe-chain-full-main-c21-20261006.log. Its SHA256 is
c1482a208efa2b912930fa45ebab543511cb6dc7510e06997f9cb46e8c5abb9c.
The previous 3ebde364 baseline and interrupted/earlier head runs are not the
final pair. Clean rebase onto c21d8274 had no conflicts; all seven pre-existing
commits have equal range-diff entries. PR remains draft; old a2ad9423 CI successes
are not follow-up CI proof. Exact final head/failed-name comparison and two new
Validate successes are required before any readiness claim.

## Resumed proof-only checkpoint, 2026-10-06 14:04 ET

NOT READY. Source/test work is paused at the operator-requested checkpoint;
sole-writer ownership remains here. Dirty work is preserved, not committed or
pushed. Committed HEAD remains aaac903e56dfd40610a7d429162ef562e73c689e on
codex/rpg-one-leg-rest-recovery. Base remains c21d8274; prior clean rebase had
no conflicts and all seven existing commits were range-diff equal. Remote draft
#1099 remains a2ad9423. No merge, deployment, broker call, service action, or
production DB write. No defensible READY ETA as of this dated checkpoint.

This section supersedes the old boolean-marker policy above. Current dirty
source uses structured identity-bearing terminal reports and a shared OMS/v2
ownership helper. Every refused/expired replacement requires positive proof,
regardless of reason; absence/status/age is not clearance. Legacy audit recovery
requires exact durable client-abort intent identity and an allowlisted pre-wire
code, not a journal refusal reason. Same-phase feedback keys include revision,
replacement generation and proof; older revisions cannot overwrite newer cache.
Zero publication rechecks terminal proof and fill/status/report evidence under
the order/journal lock. Wrong order identity is checked before fill adoption.
Positive replacement quantity stays no-rebuy even without a fill price.
Proof-only startup/new-evidence ticks claim one bounded read per evidence edge
and cannot execute old authorization. ROUNDUP's existing wire-proof gate is now
account-scoped. Per-leg skips include account/reason/generation. These resumed
changes are NOT yet a verified complete integration.

Recorded c9 audit was initially absent from the chain fixture, not absent from
production. A bounded read-only capture recovered its exact account, quantity408,
event b95fc559-b0a2-5b1b-8a78-63b252021667, generation c9b13cc4:0 and client-abort
origin. AIXI's exact corresponding audit is also captured. Raw:
/tmp/codex-t43-exact-legacy-abort-audits-20261006.jsonl (two exact intent UUIDs,
BEGIN READ ONLY, timeout5s, LIMIT2). Five older census client-abort audits were
also captured to /tmp/codex-t43-census-legacy-abort-audits-20261006.jsonl with
five exact event UUIDs, same read-only/timeout contract, LIMIT5; not yet integrated
into the census fixture. No live cache is reconstructed by these captures.

Completed resumed receipts, in order:
- Initial changed focus:2 FAIL/95 PASS, 19.50s; old marker expectation and a
  same-revision fill control, /tmp/codex-t43-proof-resume-initial-20261006.log.
- Commit-race controls:1 FAIL/112 PASS, 21.53s; revision case emitted normal
  CANCELs, not BUYs. It now isolates BUY admission without weakening the CAS
  no-zero/no-draft/sibling-invariance assertions.
- Intermediate focus:119 PASS, 22.06s,
  /tmp/codex-t43-proof-resume-focus2-20261006.log. This predates ALL terminal
  reasons being proof-gated and is not final-source proof.
- Strict all-terminal focus:12 FAIL/160 PASS, 54.32s,
  /tmp/codex-t43-all-terminal-proof-checkpoint-20261006.log. Failures expose
  missing audit inputs/isolated-control composition in chain, T43 and census
  fixtures. Latest captured-audit integration has NOT been rerun.
- Scoped source Ruff passed before the final fixture/helper edits; diff check
  passes. Old35/35 assertion mutations are not proof for resumed source: targets
  must be rebound to the new shared helper and supplemented with later-proof,
  identity/fill-CAS and reason-variant controls. No resumed full-head run started.
  Completed c21 baseline47 FAIL/6447 PASS remains available; no final paired
  failed-name comparison or new-head CI successes exist.

Remaining blockers (3):
- B1: freeze/rerun exact recorded OLOX/AIXI audit integration, all-terminal
  negative controls, SCKT positive/negative/FILLED, ROUNDUP isolation, and
  bounded evidence-edge recovery. Census precheck/deferred no-wire rows cannot
  be waived from reason/status alone; their exact positive disposition still
  needs integration/review. Do not restore old all14 unowned expectations blindly.
- B2: bind and rerun all assertion mutations on current code, including
  same-phase later revision and invalid filled identity, and commit-boundary
  status/Fill/broker-report/revision/generation races. Real PostgreSQL lock
  interleaving remains UNMEASURED; SQLite controls are not PG proof. Recorded
  live expired-report shape also remains UNMEASURED (explicit-zero test is
  synthetic). No final safety claim for unmeasured paths.
- B3: only after coherent focused/mutation proof, freeze source and run final
  head against completed exact c21 baseline, compare actual failed names,
  commit and publish draft with fresh expected-SHA lease if rebased history is
  approved, then obtain both new Validate successes. No fabricated new head.

Exact dirty write set (19 paths, rooted at
/Users/velkris/.codex/worktrees/rpg-one-leg-rest-recovery/project-mai-tai):
- docs/review-artifacts/rpg-one-leg-rest-recovery/BUILD_REPORT_2026-10-06.md
- docs/review-artifacts/rpg-one-leg-rest-recovery/check_mutations.py
- docs/review-artifacts/rpg-one-leg-rest-recovery/check_generation_countercontrol.py
- ops/health/fanout_identity_acceptance.py
- src/project_mai_tai/oms/atr_reprice_handoff.py
- src/project_mai_tai/oms/atr_reprice_runtime.py
- src/project_mai_tai/oms/service.py
- src/project_mai_tai/services/control_plane.py
- src/project_mai_tai/strategy_core/schwab_1m_v2.py
- tests/unit/test_control_plane.py
- tests/unit/test_fanout_identity_acceptance.py
- tests/unit/test_rpg1_runtime.py
- tests/unit/test_rpgstuck1_all_on.py
- tests/unit/test_rpgstuck1_later_startup.py
- tests/unit/test_t43_one_leg_recovery.py
- tests/unit/test_t43_olox_cancel_chain.py
- tests/unit/t43_recorded_audit_support.py
- tests/fixtures/t43_olox_cancel_chain_20261006.json
- tests/fixtures/t43_exact_legacy_aborts_20261006.json

## Final local proof-only receipt, 2026-10-06 14:50 ET

NOT READY: current local proof is complete; fresh follow-up CI is not. No push,
merge, deployment, restart, broker action or production DB access occurred in
this resume. Source/test ownership remains here and the tested patch is frozen.
No READY ETA is asserted before draft publication authorization and both fresh
Validate successes. Old a2ad9423 CI successes are explicitly excluded.

B1 and B2 are resolved locally. All seven captured exact legacy client-abort
audits are integrated through the real OMS journal reader. Exact captured
skipped-before-submit/precheck evidence uses that same identity/quantity/generation
contract; no MIRROR behaviour was expanded. The older five-deferral capture
omits APUS bd6's final e299 attempt, so it stays owned until that independently
recorded E3 audit is supplied. Original expected two-primary/three-mirror drafts
remain asserted; absence/reason/status does not manufacture clearance.

The runtime regression fixes preserve replacement generation fencing while
allowing exact original untagged cancellation accounting. Uncertain-submit
readback binds the newly learned broker ID; if durable event publication moves
the revision, it consumes fresh journal proof rather than retrying a stale CAS.
The 50 uncertain Webull fill repetitions remain covered. SCKT legacy refused
rows reconcile from exact successor proof, while the recorded Fill stays owned.
Healthy accepted placements own BUYs but remain serial-cancel eligible.

An additional synthetic control exposed positive canonical filled_quantity on
a cancellation report being ignored by the metadata-only reader. Both quantity
forms are now checked at first evaluation and under commit lock. Positive quantity
remains sticky no-rebuy; malformed/nonfinite/negative quantity stays UNKNOWN,
not an invented fill or zero proof. The canonical-field control is not evidence
of a production cause or of a recorded live report shape.

Current OLOX reconstructed 10:16 GREEN receipt:
stored placed token48c765e5-a03a-5fe7-b488-78355518da16; replacement generation
48c765e5-a03a-5fe7-b488-78355518da16:0; journal now refused with exact zero proof;
state generation empty, retired generation retained, active=true, quantity0,
primary_blocked=false, primary drafts1/mirror drafts0. Raw:
/tmp/codex-t43-final-green-latch-replay-20261006.jsonl,
SHA2569be175b5bcbdc0f08531bbae5e05bc07fea5aede28a34891f63de55b82fad4f8.
Historical-method counter-control again gives matching generation0/0 and only
breaking equality1/0; raw /tmp/codex-t43-current-generation-countercontrol-20261006.log.
Actual live cache remains UNMEASURED.

Final frozen receipts (old119/35 and intermediate1206/47 excluded):
- Focus:1212 PASS, 136.09s, /tmp/codex-t43-final-proof-focus-20261006.log,
  SHA256955fbe1183594ff2e350fda2e903c5b34f267b098d9c3b4006e88d415dd981e9.
- Mutations:50/50 assertion-RED, no setup failures or survivors;
  /tmp/codex-t43-final-proof-mutations-20261006.json,
  SHA256b141ec83418e44981de086e304b42eed0cfc887688e9222ee502102917c072a1.
- Full head:47 FAIL/6591 PASS, 326.35s,
  /tmp/codex-t43-final-proof-full-head-c21-20261006.log,
  SHA256c99998eecdf57e716ba0cc5e16a019684e19dfd893ccbe6116491e86c1e62f3a.
- Exact base c21d8274:47 FAIL/6447 PASS, 291.24s, raw/hash above.
  Both actual sorted 47-name lists are identical (diff exit0), SHA256
  fe24b2de3074d47d6104576c5d4392a4ac85e0fda55b246755703dc903859c50.
  /tmp/codex-t43-final-{base,head}-failed-names-20261006.txt.
- Ruff src/tests/scripts, diff check and marker isolation pass. The source/test
  manifest was verified unchanged across focus, mutations and full run:
  /tmp/codex-t43-final-proof-source-manifest-20261006.txt,
  SHA2568ee79389724a640231e08792ee72328359d8bdf39700909d9d4924ad291f258d.

No source/test edit occurred during the final full-head run. Current base remains
c21d8274; no further rebase. Rechecked range-diff: all seven prior commits equal,
no conflicts. Remote draft still a2ad942383d8b2ece1b63696ce56a16e9568a992 by fresh
ls-remote read. The codex worktree marker hook remains installed.

One remaining gate, B3: draft-only publication and both new Validate successes.
Publication was not attempted under the current restriction. Real PostgreSQL
lock interleaving and recorded live expired-report shape remain UNMEASURED;
SQLite commit-boundary tests and synthetic explicit-zero expiry do not replace
those measurements or attest every terminal report shape. Unknown ownership
still fails closed. No review-ready/activation claim.

Exact changed set is25 paths: the19-path list above plus these six test-only
composition/recorded-proof integrations (no extra source scope):
- tests/unit/test_pmprint1_tonight_flags.py
- tests/unit/test_roundup1.py
- tests/unit/test_rpgstuck1.py
- tests/unit/test_rpgstuck1_startup.py
- tests/unit/test_rpgstuck1_loop_liveness.py
- tests/unit/test_rpgstuck1_review_completion.py
