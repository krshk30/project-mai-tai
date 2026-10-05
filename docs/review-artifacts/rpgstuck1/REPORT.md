# RPGSTUCK1: canonical prices and proven-clear handoff endings

Latest runtime follow-up: [R20 exhaustion-edge wake](REPORT_R20.md).

Latest fixture-only follow-up: [connection-isolation proof](REPORT_FIXTURE_ISOLATION.md).

Latest rebased review follow-up: [complete B1-B6 report](REPORT_B_REVIEW.md),
with merged PM integration, later14-ticket census and fresh paired verification.
Earlier reports below retain their historical base/head and as-of evidence.

Scope: sole-writer branch `codex/rpgstuck1-canonical-handoff`, starting at
`a80b51816abf0aefc269f0fdfc473468fd3fe62c`. The October 5 own assessment at
`3e3fc738` was explicitly acknowledged before this build. Its corrected causes,
the original card, and the operator's corrected requirements govern this change.
OWNMIX1 and GAPKEEP are separate lanes; no shared handoff edit is included.

**Latest explicit sequencing:** RPGSTUCK1 proceeds at normal pace as a standalone
later change, **NOT tonight**. This supersedes the initial joint evening-plan
references. The follow-up adds only tests, fixtures and report/evidence artifacts
on top of `73e9e4c8f92f672ecc03e7c460b134123015840a`, with no rebase and no PM edits.
Tonight's OFF proofs are a separate parent-owned task, not RPGSTUCK1 semantics.

## Recorded Before / After

`tests/fixtures/rpgstuck1_recorded.json` extracts all four E1 persisted tickets
and their exact E3 authorizations, including VEEA Webull's NULL authorization.
The fixture carries both evidence SHA256 values. `capture_fixture.py` reproduces
the extraction from `/Users/velkris/.codex/ownmix-rpgstuck-evidence-20261005`.
No ticket phase, execution quantity, or recorded price is rewritten in the fixture.

| Exact ticket | Recorded phase / old-order proof | Main replay | This build replay |
| --- | --- | --- | --- |
| APUS primary `fbfd692d-ec1f-5a39-9e11-1a133abccc96` | refused; strict zero-fill clearance at 1791207124.1614 | Owns the entry for the same segment | Releases this account for normal placement |
| APUS Webull `bd6ac0b9-727c-500b-8581-aabdd95992d4` | refused; strict zero-fill clearance at 1791207126.99776 | Owns the entry for the same segment | Releases this account for normal placement |
| VEEA primary `a007716c-4b50-5759-a354-ddc5b8961154` | refused; strict zero-fill clearance at 1791207363.984321 | Blocked by global ownership | Can place while Webull remains unresolved |
| VEEA Webull `faa55c1f-262b-52e2-adcc-280ab1e5e2ff` | held_unknown; local_no_wire=false; no strict broker proof | Blocks both accounts | Blocks only Webull until exact proof is recovered |

There were **zero durable requested tickets** in this population. APUS refused /
refused makes global ownership true on main and false here. VEEA refused /
held_unknown keeps global ownership true here, but primary account ownership is
false and Webull ownership remains true. See `recorded-before.json` and
`recorded-after.json`; baseline has no per-account API, so its account entries
represent the original global veto applied to both placement queues.

| Recorded authorization | Existing canonical outgoing price | Quantity | Main guard | This build guard |
| --- | --- | --- | --- | --- |
| APUS stop 5.2720 / limit 5.2983 | 5.27 / 5.30 | 113 | rpg_current_price_size_or_identity_changed | accepts unchanged canonical price / size / identity |
| VEEA stop 5.5093 / limit 5.5368 | 5.51 / 5.54 | 108 | rpg_current_price_size_or_identity_changed | accepts unchanged canonical price / size / identity |

The guard uses the existing float formatter and collapsed-band lift used by the
native Schwab bracket and dollar sizing. It still checks canonical stop and limit,
quantity, flip level, entry slot, segment, fanout slot, account, symbol, client ID,
authorization age, entry window, and original-fill evidence. It introduces no
price tolerance. APUS Webull's **9.7117%** distance refusal is still reproduced
before any simulated wire call.

Future bars, fresh allowable quotes, clocks, empty setup databases, later broker
answers and acceptance prices are explicitly **controlled simulations**. The
canonical replay proves the OMS guard correction, not a historical venue acceptance
or measured post-fix live latency. No lost-opportunity P&L is estimated.

## Ownership And Restart Proof

Refused / expired tickets release only when their old BUY has a durable
`cleared_at` or explicit `local_no_wire=true` and no old fill. Unproven terminals
still block in both strategy placement and OMS's final tokenless-open guard.
Requested acknowledgements remain generation-bound; missing account feedback is
not clearance. Normal placement selects accounts independently. Already accepted
RPG legs are fenced by their replacement generation, not stale share counters.
This preserves confirmation release, retry budgets, and new opportunity rotation.

Distance-deferred state now records no-wire evidence only at the actual precheck.
The marker becomes false immediately before **that account's** wire await; a
Schwab wire sharing the fanout slot cannot invalidate Webull's local proof.
NFQ held / queued and distance-held generations can be retired without inventing
a broker cancellation. Old queued deliveries lose their exact slot claim.

Restart recovery accepts only an exact account / strategy / symbol / generation /
slot / segment match with a durable `skipped_before_submit` distance refusal.
Every intent for that generation must supply that evidence, and any BrokerOrder
for it blocks the no-wire inference. A later submitted or uncertain intent vetoes
an earlier precheck refusal. Deferred retries now commit their exact client ID
before the wire await so a lost response cannot masquerade as an empty book.
The recorded VEEA no-wire open client ID is recovered as
`schwab_1m_v2-VEEA-open-2d004a27acd7`, without a cancel or broker read.

Unknown replacement dispatch is reconciled through the existing strict broker
reader using the claimed replacement client ID. Committed working, cancelled-zero,
or filled detail is booked against that replacement; its submit is never replayed.
The existing one-second spacing, two-second read bound, and thirty-read budget are
reused. Exhaustion / absence / read failure stays blocking. Missing old identity
is not cleared by age. No new timeout, empty-list proof, or phase relabel is used.

Flip / consumed slot / entry or gap hold ends a **cleared** ticket as expired.
An unknown old order remains owned even if entry gates later close. Admission gates
are evaluated on every call, including changed evidence in the same bar; only the
log is limited to once per wall-clock one-minute bar per symbol.

## Initial Named Verification (73e9e4c8)

| Card | Exact tests / executable coverage |
| --- | --- |
| R-T1 APUS next bar | `test_r_t1_actual_apus_refused_tickets_release_next_bar_both_accounts`: both simulated broker placements; twenty subsequent bars retain the rests with no extra drafts |
| R-T2 VEEA / independent legs | `test_r_t2_actual_veea_unknown_leg_does_not_block_cleared_primary_next_bar`; `test_r_t2_distance_hold_is_local_only_before_wire_and_old_queue_retires` also sends the other account while preserving the local hold; inherited `test_one_unproven_leg_does_not_block_other_legs_confirmed_replacement` covers both healthy-account directions |
| R-T3 twenty bars / fresh gates | R-T1 twenty-bar loop plus `test_r_t3_twenty_bars_admission_logs_once_but_gates_are_fresh_each_call`: twenty log lines, 220 fresh allowed/refused evaluations |
| R-T4 partial fill / no rebuy | inherited `test_controlled_partial_race_accounts_original_buy_and_protects_actual_position` for both brokers; `test_recorded_webull_full_fill_is_original_buy_not_replacement`; strict readback / coordinator tests retained |
| R-T5 restart / uncertain dispatch | `test_r_t5_actual_veea_persisted_precheck_proof_recovers_after_restart` includes the later uncertain-intent veto; `test_r_t5_uncertain_webull_dispatch_reconciles_exact_client_without_resubmit` covers unknown, working, explicit cancelled-zero and fill; `test_distance_retry_persists_exact_client_before_an_uncertain_wire`; inherited restart / bounded-unknown tests |
| R-T6 flag off | inherited `test_switch_off_preserves_inflight_cancel_and_restart_ownership`, `test_rpg_flag_off_new_reprice_retains_legacy_cancel_then_next_pass` and original flag-off cases in the broad/full suite |
| R-T7 NFQ / PMREST / PMPRINT / PMFLIP | All existing RPG + NFQ modules, PMREST production path and full PMPRINT/PMFLIP test module in a disposable composition checkout, specified below |
| R-T8 addendum / B6-B8 | `test_r_t8_recorded_e3_authorization_matches_actual_cent_wire`; `test_r_t8_canonical_real_difference_is_still_refused` (both recorded symbols x eight changed fields); `test_terminal_release_requires_old_order_clear_proof`; `test_r_t8_b6_b8_cleared_ticket_ends_at_flip_slot_or_hold` |

The older runtime refusal test is renamed to
`test_explicit_simulated_broker_refusal_releases_only_proven_clear_leg`; its
obsolete permanent-block assertion is replaced by the authorized next-placement
expectation. No test is skipped or xfailed to mask a changed outcome.

`run_focused.py` preserves the broad original RPG coverage (including v2, sizing,
fanout, backtest and all three RPG/NFQ composition families), adds this recorded
module and PMREST, and the final command also includes confirmation, retry-one,
resting-cancel and Webull-mirror suites. The new recorded module has **46 cases**.
Full paired counts, exact failed IDs, selected modules, XML hashes and frozen
source/test hashes are recorded in `verification.json`.

| Final suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Own exact a80b5181 main unit baseline | 5,412 | 56 | 0 |
| Frozen RPGSTUCK1 unit build | 5,458 | 56 | 0 |
| Broad focused regression | 1,108 | 0 | 0 |
| Pinned PMPRINT composition | 587 | 0 | 0 |

The paired failure-name sets are identical: zero added and zero removed failures.
The own baseline's exact failed lines also match `/tmp/pmprint-main-unit.txt`.

| Frozen production file (under `src/project_mai_tai/`) | SHA256 |
| --- | --- |
| `oms/atr_reprice_handoff.py` | `08bf0e12bfe331e6bd4b7f527f38aceeecfaa40348586426e0af775da686886c` |
| `oms/atr_reprice_runtime.py` | `d80001f1c8ab0ba192495f721f033799fdaa8d934c9a4741372a76758e76bcb5` |
| `oms/service.py` | `112d07f039de2dfcbb0d96551fa1dedeed76735c6200a0214328b35a7488a399` |
| `strategy_core/schwab_1m_v2.py` | `6a3d477aae041b3d87fa18d72c05665a7794e0d5dd480104302ff7fe14ab3f5e` |

### Reproduction Commands

All Python runs use `PYTHONPATH=src` and
`PY=/Users/velkris/Projects/project-mai-tai/.venv/bin/python`, from the specified
isolated checkout. Exit 1 on each full unit suite is the retained 56 failures.

```sh
# Own a80b5181 baseline checkout
"$PY" -m pytest tests/unit -q --junitxml=/tmp/rpgstuck-main-git-full.xml
# Sole-writer RPGSTUCK1 checkout, frozen source
"$PY" -m pytest tests/unit -q --junitxml=/tmp/rpgstuck-head-reviewed-full.xml
"$PY" docs/review-artifacts/rpgstuck1/run_focused.py \
  tests/unit/test_v2_entry_composition_slot.py tests/unit/test_v2_flip_owned_first_entry.py \
  tests/unit/test_v2_resting_cancel_rth_placed.py tests/unit/test_v2_retry_one.py \
  tests/unit/test_v2_webull_resting_mirror.py --junitxml=/tmp/rpgstuck-focused-reviewed.xml
"$PY" docs/review-artifacts/rpgstuck1/check_mutations.py
"$PY" docs/review-artifacts/rpg1/check_runtime_mutations.py
# Disposable pinned 71f5f6bc composition checkout with generated RPGSTUCK1 patch
"$PY" -m pytest -q tests/unit/test_rpg1_*.py tests/unit/test_rpgstuck1.py \
  tests/unit/test_nfq1_mirror_fresh_price.py tests/unit/test_pmrest1.py \
  tests/unit/test_pmprint1_pmflip1.py tests/unit/test_schwab_1m_v2_eh_stream_cross.py \
  tests/unit/test_oms_webull_mirror_deferred_resubmit.py tests/composition/test_rpg1_nfq1.py \
  --junitxml=/tmp/rpgstuck-composition-reviewed.xml
```

Mutations: all **nine new** falsifiers are assertion-killed in isolated imported
memory, plus all **nine original runtime** mutations remain assertion-red. New
mutations restore raw-price comparison, drop canonical-price differences, release
without clearance, relabel a dispatched distance hold, flood logs, cache admission
permission, restore the same-segment terminal latch, ignore an uncertain durable
intent, and omit the deferred retry's durable claim. Import/anchor errors do not
count as kills. See `mutations.txt` and `original9-mutations.txt`.

The paired baseline was independently run in `/tmp/rpgstuck-main-git-pair` at the
exact supplied main SHA. Its **56 failed / 5,412 passed** also agrees with the
operator's retained `/tmp/pmprint-main-unit.txt`. An initial archive lacked Git
metadata and failed the executable-index test; it is superseded by this checkout.
Intermediate heads that caught overbroad share-counter fences are superseded by
the frozen source whose hashes are in `verification.json`.

Initial raw files: `/tmp/rpgstuck-main-git-full.txt/xml`,
`/tmp/rpgstuck-head-reviewed-full.txt/xml`,
`/tmp/rpgstuck-broad-reviewed.txt`, `/tmp/rpgstuck-focused-reviewed.xml`, and
`/tmp/rpgstuck-composition-reviewed.txt`. Full source/test Ruff and whitespace
checks pass. Existing local-platform failures are not a green full-suite claim.

## Recorded Wire-Level Follow-Up

The requested end-to-end cases are in
`tests/unit/test_rpgstuck1_schwab_sequences.py`. They execute the **real** v2
authorization callback, bot feedback persistence, RPG controller, ordinary OMS
`process_trade_intent`, native-OCO decorator, both final-wire guards, and Schwab
`submit_order` / bracket payload builder. Replacement HTTP transport is intercepted
at `_authorized_request_json` before token refresh or any HTTP call. Inputs are not pre-rounded
and the guards are not replaced with an unconditional permission.

| Exact test / sequence (ET) | Actual strategy output | Actual intercepted Schwab payload | Outcome |
| --- | --- | --- | --- |
| `test_recorded_0932_0936_strategy_authorization_to_schwab_wire[APUS-0932]` | Recorded 09:32 probe trail5.245742 regenerates E3 stop5.2720 / limit5.2983, qty113 | STOP_LIMIT TRIGGER -> OCO, stopPrice5.27 / price5.30, all legs113 | One simulated accepted replacement, durable phase placed; repeated feedback/tick emits no second POST |
| `test_recorded_0932_0936_strategy_authorization_to_schwab_wire[VEEA-0936]` | Recorded 09:36 probe trail5.481886 regenerates E3 stop5.5093 / limit5.5368, qty108 | STOP_LIMIT TRIGGER -> OCO, stopPrice5.51 / price5.54, all legs108 | One simulated accepted replacement despite the recorded unresolved Webull ticket; Webull stays owned; no second POST |
| `test_recorded_apus_1025_1026_1027_place_keep_reprice_to_schwab_wire` | 10:25 trail5.295484 -> stop5.3220 / limit5.3486, qty112; 10:26 trail5.291387 stays below reprice threshold; 10:27 trail5.213060 -> stop5.2391 / limit5.2653, qty114 | Initial stopPrice5.32 / price5.35; no new payload at10:26; replacement stopPrice5.24 / price5.27 | Initial POST, then exactly one strict terminal-zero read and one replacement POST; old112 cancelled / new114 accepted in isolated DB; repeat tick produces no extra POST |

The first two cases rewind their **recorded, proven-clear** tickets before their
later historical refusals; the new four-decimal authorization is regenerated,
not injected from the saved authorization. Raw stop, limit, line, segment, slot,
quantity and outgoing cent prices are independently asserted. The late case runs
the normal initial placement and actual reprice cancellation path before the same
authorization/rounding/serialization chain. Its Webull 10:25 market4.56 /
stop5.3220 (**14.3179%**) hold is recreated through the ordinary precheck with the
recorded OMS cached ask/time; no mirror wire is permitted. At10:27 that old mirror
is proven local and retired without a broker cancellation, independently of the
primary's strict read. This new case selects only Schwab's replacement delivery;
the existing R-T2/R-T5 tests continue to cover mirror reauthorization/dispatch.

New late evidence: read at **2026-10-05T15:24:12.411211Z**, read-only transactions,
8-second query bounds, ticket/order/intent limits4/10/20, APUS only,14:25-14:28Z.
`read_late_sequence.py` ran via stdin at nice19 and was not installed remotely.
The companion log pull reads only the selected APUS markers in that interval.
`sequence-late-sql.json` and `sequence-late-logs.txt` retain both late tickets,
their exact payloads and the observed lines. `rpgstuck1_sequences.json` preserves
their SHA256 provenance and the two earlier retained probes. The earlier four-
ticket census above remains its original frozen population, not a claim that no
later tickets existed.

Quotes not recorded in the ATR probes, bar OPEN values, empty test database setup,
venue acceptance, the deliberately lost late cancel ACK, and the late strict-reader
response are **controlled simulation**.
The recorded ticket's clearance remains the provenance for early replay staging.
The OCO geometry is configured to the recorded +5% / -8%; no production setting is
changed. These tests prove serialization and dispatch count, not historical venue
acceptance, live post-fix latency or live P&L. There are no live broker calls or
ledger repairs.

Three additional assertion-killed mutations prove these new cases detect restored
raw authorization comparisons, missing **actual OMS stop rounding**, and missing
**actual OMS limit rounding**. See `check_sequence_mutations.py` and
`sequence-mutations.txt`; mutation anchor/import errors do not count as kills.

| Fresh follow-up suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Own exact a80b5181 main unit baseline | 5,412 | 56 | 0 |
| Test-only follow-up unit build | 5,461 | 56 | 0 |
| Broad focused including all three wire cases | 1,111 | 0 | 0 |
| New recorded wire-level sequence module | 3 | 0 | 0 |

The two fresh full-suite failure-name sets are **identical** to each other and
the initial56: no added or removed failed IDs. The fresh baseline also matches
the operator's retained `/tmp/pmprint-main-unit.txt` exact failure lines. All
**21/21** mutations are assertion-killed: the original18 rerun plus the three new
wire-level falsifiers. `followup-mutations.txt` and `followup-original9-mutations.txt`
retain the new original18 runs; no prior logs are overwritten.

### Fresh Follow-Up Commands

The fresh pair uses `/tmp/rpgstuck-main-git-pair` at the original a80b5181 for main
and the sole-writer checkout for the build. No branch is rebased. Each full-suite
exit1 is the retained baseline failures, not a green full-suite claim.

```sh
export PYTHONPATH=src
PY=/Users/velkris/Projects/project-mai-tai/.venv/bin/python
# Own exact a80b5181 baseline checkout
"$PY" -m pytest tests/unit -q --junitxml=/tmp/rpgstuck-followup-main-full.xml
# Sole-writer checkout
"$PY" -m pytest tests/unit -q --junitxml=/tmp/rpgstuck-followup-head-full.xml
"$PY" -m pytest tests/unit/test_rpgstuck1_schwab_sequences.py -q \
  --junitxml=/tmp/rpgstuck-followup-sequences.xml
"$PY" docs/review-artifacts/rpgstuck1/run_focused.py \
  tests/unit/test_v2_entry_composition_slot.py tests/unit/test_v2_flip_owned_first_entry.py \
  tests/unit/test_v2_resting_cancel_rth_placed.py tests/unit/test_v2_retry_one.py \
  tests/unit/test_v2_webull_resting_mirror.py tests/unit/test_rpgstuck1_schwab_sequences.py \
  --junitxml=/tmp/rpgstuck-followup-focused.xml
"$PY" docs/review-artifacts/rpgstuck1/check_sequence_mutations.py
"$PY" docs/review-artifacts/rpgstuck1/check_mutations.py
"$PY" docs/review-artifacts/rpg1/check_runtime_mutations.py
```

Logs use the same `/tmp/rpgstuck-followup-` stems with `.txt` instead of `.xml`.
`verification-followup.json` preserves this fresh pair, exact new test IDs, failure
IDs, XML hashes and final production/new-test/fixture SHA256 values without
overwriting the initial evidence. Production SHA256 values must equal all four
initial production hashes listed above. Ruff and whitespace checks pass.

## Complete Startup Follow-Up

The newer human-requested follow-up is recorded separately in
`REPORT_STARTUP.md`, with all eight October5 tickets, all five deferred intents,
four actual strict Schwab response bodies, exact OFF/outside-window behavior,
test-revealed narrow source corrections and fresh paired verification.
Its final source hashes supersede the historical hashes above for the new head;
the earlier test-only verification remains its original frozen evidence.

## Composition And Deployment Boundary

NFQ1 and PMREST1 are already included in base a80b5181. PMPRINT1 / PMFLIP1 is
**unmerged PR #1092**, head
`71f5f6bc5ae63a2a02b4a6fe5ed469b71cfbe1c8`. Its actual source and tests were composed
by applying this lane's generated patch to a new disposable checkout of that exact
head; all three-way applications were clean. The dependency's own worktree and
branch were read-only. The final focused composition has **587 passed**; the
dependency is not cherry-picked into this PR. GAPKEEP remains assessment-only
and has no source to compose. OWNMIX1 source composition is not claimed.
This composition result belongs to the initial 73e9e4c8 verification; the later
test-only standalone follow-up does not repeat it or claim PM coverage for its
three new cases. No PM source or PM branch was modified in this follow-up.

Proposed restart list for a separately approved standalone later deployment:

- `project-mai-tai-oms.service`: runtime, final-wire guard and deferred claim changes.
- `project-mai-tai-strategy.service`: companion restart in the tracked OMS deploy path.
- `project-mai-tai-schwab-1m-v2.service`: ownership, fresh admission logging and gate endings.

Merge order relative to PMPRINT is not a runtime dependency: the exact source
composition is clean, but any final combined pin must be independently verified
after integration. RPGSTUCK1 is explicitly **excluded from tonight**. A later plan
still requires its own operator GO, flatness proofs, exact pinned heads and scanner
validation. This PR does not approve or execute a deployment. There were **zero production service actions,
broker trades, ledger writes, or merges** in this lane; database writes are confined
to isolated test fixtures. Configured entry windows, distance thresholds, bands,
dollar sizing, one-entry-per-flip and strict cancel/readback semantics are retained.
