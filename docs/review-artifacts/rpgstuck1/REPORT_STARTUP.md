# RPGSTUCK1 Complete Recorded Startup Follow-Up

Parent head: `289524631f91f26e922a964067f3ef9907cbc188`.
Base: `a80b51816abf0aefc269f0fdfc473468fd3fe62c`. No rebase.
One follow-up commit, with two narrowly authorized source corrections after new
tests failed. No production actions, ledger repairs, broker trades, other-branch
edits, shared-handoff edits, deployment approval, or merges.

## Recorded Population And Proof

The complete read at **16:13:20.652236Z** contains **eight tickets, five old broker
orders, and all five deferred intents**. The source query also included exact
generation-bound broker-order searches. Nothing is filtered to omit the later
VEEA/APUS refusals. The four exact broker GET bodies at **16:13:55.182715Z** are
retained in `tests/fixtures/rpgstuck1_startup_broker.json`: three CANCELED-zero
parents and RETO's REJECTED parent, quantity252, filledQuantity0, remainingQuantity0.
Fixture provenance includes source paths and SHA256 values.

All following startup clocks, fresh bars/quotes, broker transport and subsequent
acceptances are isolated simulations. The actual recorded response bodies and
durable payloads are unchanged. This is not a measured future production startup.

| Ticket | Recorded State | Recovery Disposition |
| --- | --- | --- |
| APUS Schwab `fbfd692d-ec1f-5a39-9e11-1a133abccc96` | refused; old CANCELED, durable cleared_at | No RPG veto on primary next placement |
| APUS Webull `bd6ac0b9-727c-500b-8581-aabdd95992d4` | refused; old CANCELLED, durable cleared_at; replacement attempt3 precheck-deferred | This old ticket is clear; later `ee3d0d07` still blocks the account until separately proven |
| VEEA Schwab `a007716c-4b50-5759-a354-ddc5b8961154` | refused; old CANCELED, durable cleared_at | No RPG veto on primary next placement |
| VEEA Webull `faa55c1f-262b-52e2-adcc-280ab1e5e2ff` | held_unknown; no old target; initial no-wire plus later ownership refusal | Recover exact original open client `schwab_1m_v2-VEEA-open-2d004a27acd7`; clear and still owned until existing transaction finishes |
| APUS Schwab `7c7c5afb-30ff-539f-8baa-4c2e585c6e12` | refused; old CANCELED, durable cleared_at | No RPG veto on primary next placement |
| APUS Webull `ee3d0d07-abea-5fe2-98c9-cace5c08d135` | held_unknown; no original_order_id/client target; initial no-wire plus later ownership refusal | Recover exact original open client `schwab_1m_v2-APUS-open-38a0c5c6f90d`; clear and still owned until existing transaction finishes |
| RETO Schwab `ff6464ff-d65e-5657-8f47-1dc8d7b053c3` | held_unknown; reads30; old DB rejected, broker1008171127230 | One exact strict REJECTED-zero proof read ends ticket as refused/cleared; DB rejection alone never releases |
| RETO Webull `ba108172-04f6-5659-892b-a0fc10d22b15` | held_unknown; initial distance-precheck no-wire | Recover exact original open client `schwab_1m_v2-RETO-open-41cf913d0c96`; clear and still owned until existing transaction finishes |

`startup-dispositions.json` records the before, proof-stage and finished ownership
states. The collector ends local tickets through a controlled current entry hold,
without submitting orders. Placement permission in this report means **no RPG
ownership veto**, not bypassing NFQ, current entry gates or RETO's broker policy.

## New Failures And Narrow Corrections

The original source failed three new assertions: complete deferred-intent recovery,
strict decoding of the actual RETO zero rejection, and startup recovery of its
exhausted ticket. `/tmp/rpgstuck-startup-broker-before.txt` retains this red run.

1. `_rpg_persisted_local_open` now tolerates only exact same-identity retry rows
   rejected with `client_abort/rpg_old_buy_still_owned`, source `oms-risk`, explicit
   deferred retry metadata and a predecessor chain anchored in a proven distance
   precheck. Those guard rows do not replace the original proven no-wire client.
   Any submitted/unknown row, identity difference, missing anchor or generation-
   bound BrokerOrder still vetoes local proof.
2. Schwab's strict reader recognizes `rejected_empty` only after existing parent,
   BUY-leg, quantity and execution checks, with explicit zero filled AND remaining.
   This is **not** a cancellation and `can_replace` remains false for rejection.
3. An exhausted ticket with an exactly bound committed rejected old order gets
   **one separate lifetime proof-only GET**, durably claimed before await. The
   original reads30 remains unchanged. This is a deliberate new terminal-proof
   exception, not a renewed cancel budget or periodic unknown retry. Two-second
   read timeout remains. Missing answers, stale/crash claims and identity mismatch
   remain blocking; restart never repeats the GET. Successful proof ends the old
   ticket, never sends a cancel or a saved replacement, and leaves the old DB row
   rejected. A fill committed during GET vetoes clearance. Positive fills with
   exact price use the existing original-BUY accounting path and retain no-rebuy;
   absent execution price is explicitly `UNMEASURED`, stays owned, and is never
   booked at a guessed price.

## Exact OFF Behavior And Tests

Every new startup/legacy test carries PMprintON, PMflipOFF, PMrestOFF,
RPGhandoffOFF, NFQON, GAPholdON. OFF prevents **new ticket admission**, not
abandoning already durable transactions. A `clear` local ticket is still owned:
it blocks tokenless/legacy competition until placed or expired. Fresh authorization
may complete its existing replacement during the configured window. Three recovered
Webull legs place once, repeated feedback/tracking does not duplicate them, and the
ticket population stays eight. First/reclaim new reprices remain legacy
cancel-then-next-pass, with GAP hold still cancelling both legs and preventing opens.

At simulated **20:05 ET**, complete recorded proof expires the three local tickets
through the existing `window_closed` rule, clears RETO by exact rejection proof,
and emits **zero opens**. Next-day ownership has no leftover proven-ticket veto.
The paired true-unknown case remains owned next day: neither clock age nor closed
window invents clearance. No new timeout or next-day purge is introduced.

Exact named tests in `tests/unit/test_rpgstuck1_startup.py`:

- `test_real_startup_restores_all_seven_requested_tickets_and_eighth_census_guard`
- `test_real_startup_all_eight_and_all_five_deferred_intents_recover_only_proven_local_legs`
- `test_actual_four_schwab_bodies_require_exact_terminal_zero_parent` (four parents)
- `test_real_oms_startup_exhausted_reto_uses_actual_rejected_zero_readback_once`
- `test_recorded_reto_rejected_database_status_and_age_never_clear_exhausted_ticket`
- `test_recorded_local_retry_chain_never_releases_unproven_dispatch` (two symbols x ten counterfactuals)
- `test_reto_terminal_probe_unproven_or_filled_never_releases_or_retries` (nine counterfactuals)
- `test_reto_terminal_probe_crash_claim_never_repeats_get_or_releases` (three ages)
- `test_reto_terminal_probe_requires_exact_committed_old_identity` (five differences)
- `test_reto_terminal_probe_positive_fill_with_price_uses_existing_accounting_no_rebuy`
- `test_reto_terminal_zero_cannot_override_fill_accounted_during_get`
- `test_tonight_off_existing_clear_local_jobs_resume_without_legacy_duplicate`
- `test_tonight_flags_off_legacy_first_reclaim_cancel_then_next_pass` (first/reclaim)
- `test_tonight_flags_gap_hold_still_blocks_legacy_replacement` (first/reclaim)
- `test_tonight_all_eight_outside_window_startup_expires_only_proven_tickets_no_stored_buys` (recorded proof/true unknown)

## Verification And Composition Limits

Final exact counts, all 54 new test IDs, both full-suite failed-ID sets, XML hashes,
and source/test/fixture SHA256 values are in `verification-startup.json`.
The fresh paired results are:

| Run | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Exact main `a80b5181`, full unit suite | 5412 | 56 | 0 |
| Final frozen RPG source/tests, full unit suite | 5515 | 56 | 0 |
| Broad focused suite | 1165 | 0 | 0 |
| Complete recorded startup module | 54 | 0 | 0 |

The full suites have identical failed node IDs: zero added and zero removed.
Neither full suite is green. Main completed in 214.27 seconds; final RPG completed
in 228.30 seconds. The prior three end-to-end Schwab wire sequences are included
in the focused and full results, with canonical payload outcomes unchanged.
The earlier frozen-source full run preceded the final two outside-window cases;
it is superseded by `/tmp/rpgstuck-startup-head-frozen-full.txt/xml`.

The 11 new startup falsifiers and all prior21 are assertion-red, **32/32** total.
Anchor/import errors do not count. Retained logs are `startup-*-mutations.txt`.

Final production SHA256 values (paths relative to `src/project_mai_tai/`):

| Source | SHA256 |
| --- | --- |
| `oms/atr_reprice_handoff.py` | `08bf0e12bfe331e6bd4b7f527f38aceeecfaa40348586426e0af775da686886c` |
| `oms/atr_reprice_runtime.py` | `5793819f66e679bb9203b15e4d4f7bd55d1818f2c3c79960a1c4185d90d7eb0d` |
| `oms/service.py` | `112d07f039de2dfcbb0d96551fa1dedeed76735c6200a0214328b35a7488a399` |
| `strategy_core/schwab_1m_v2.py` | `6a3d477aae041b3d87fa18d72c05665a7794e0d5dd480104302ff7fe14ab3f5e` |
| `broker_adapters/atr_buy_readback.py` | `e4595a6424cc7e92c4be32257a6fabc039c4e04da630b02305fde1e41947cb78` |

PMPRINT source is **not** included in this standalone branch. Its PM flags are
explicit settings inputs here; this does not claim PM runtime composition.
Parent owns the final composition with pinned #1092 `5ee9f41654d8bb3b66414c4b84c3804c8d4c47c6`.
Its unchanged `test_current_rpg_off_startup_still_restores_recorded_blocking_ticket`
characterization has three obsolete expected-block assertions for `fbfd692d`,
`bd6ac0b9`, `a007716c`; its unproven `faa55c1f` assertion must remain blocking.
Parent independently composed immutable pinned `5ee9f416` with reviewed `28952463`
and confirmed exactly these three failures: **11 passed / 3 failed**, retained in
`/tmp/oct5-pm-rpg-characterization.txt`. That result covers the previous RPG head,
not this new startup follow-up. These are integration-test disagreements, not
skips or a green combined suite claim. After PM merge and fresh-head review, the
integration characterization must assert proof-dependent ownership (clear old
tickets release; genuinely unproven tickets block), rather than permanent
blocking. No PM-only test is copied into this standalone PR to hide the delta.

Restart scope for any separately approved later deployment remains OMS, its strategy
companion, and v2. This follow-up does not approve tonight's plan or execute it.

## Reproduction

All commands use `PYTHONPATH=src` and
`PY=/Users/velkris/Projects/project-mai-tai/.venv/bin/python`.

```sh
# Exact a80b5181 main checkout /tmp/rpgstuck-main-git-pair
"$PY" -m pytest tests/unit -q --junitxml=/tmp/rpgstuck-startup-main-full.xml
# Sole-writer RPG checkout, final frozen source/tests
"$PY" -m pytest tests/unit -q --junitxml=/tmp/rpgstuck-startup-head-frozen-full.xml
"$PY" -m pytest tests/unit/test_rpgstuck1_startup.py -q --junitxml=/tmp/rpgstuck-startup-recorded-final.xml
"$PY" docs/review-artifacts/rpgstuck1/run_focused.py \
  tests/unit/test_v2_entry_composition_slot.py tests/unit/test_v2_flip_owned_first_entry.py \
  tests/unit/test_v2_resting_cancel_rth_placed.py tests/unit/test_v2_retry_one.py \
  tests/unit/test_v2_webull_resting_mirror.py tests/unit/test_rpgstuck1_schwab_sequences.py \
  tests/unit/test_rpgstuck1_startup.py --junitxml=/tmp/rpgstuck-startup-focused-frozen.xml
"$PY" docs/review-artifacts/rpgstuck1/check_startup_mutations.py
"$PY" docs/review-artifacts/rpgstuck1/check_mutations.py
"$PY" docs/review-artifacts/rpg1/check_runtime_mutations.py
"$PY" docs/review-artifacts/rpgstuck1/check_sequence_mutations.py
"$PY" docs/review-artifacts/rpgstuck1/replay_startup.py
"$PY" docs/review-artifacts/rpgstuck1/collect_startup_results.py
```
