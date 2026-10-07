# RETRYLEFT1 review delivery

## Card and scope

After the exact owned trade closes in its causal SELL cycle and the existing
retry budget is exhausted, cancel resting buys on both configured accounts.
Regular-hours broker rest and pre-market software rest use the same existing
serial buy-only cancellation barrier. No trigger, price, sizing or exit change.
No new flag: the installed RETRY_ONE_ENABLED=true / MAX_RETRIES=0 is the policy.

The cancellation request has purpose=retry_exhausted in the existing
v2_removed_wait snapshot. Legacy scanner requests omit purpose exactly as
before. A receipt is cancellation-only: the filled owner, episode ids and
retry budget remain consumed/bound. Unknown answers remain blocking. Pending
requests restore and re-emit their exact-slot barriers after restart. Proofs
are read in the existing periodic v2 to_thread path; no OMS source/tick edit.

## Recorded acceptance

| Case | Recorded close | Cancellation emission in replay | Scope |
| --- | --- | --- | --- |
| NCPL10-07 | 19:10:10UTC | 19:10:18.069UTC, +8.069s | Recorded durable owner; Webull182 waiting, Schwab364 filled then closed |
| ARTL09-23 | 18:53:09.111UTC | 18:53:12.758UTC, +3.647s | Recorded identities/timing, explicitly reconstructed current zero-retry owner; Schwab2 waiting, Webull1 closed |
| APUS09-24 | Primary20:26:30UTC | No leftover cancellation | Both entries filled19:54:58; sibling remains a held position until its recorded later manual close |

The tests emit cancels within one bar. New actual broker-cancel latency is
UNMEASURED; acknowledgements in receipt controls are simulated and labelled.
Raw own pulls: ncpl-own-read.json, artl-own-read.json,
apus-both-filled-own-read.json; source snapshots are read-only and UTC.

Primary tests in tests/unit/test_retryleft1.py:
- test_recorded_ncpl_close_cancels_sibling_at_191018_before_next_bar
- test_recorded_artl_close_cancels_schwab_at_first_owner_check_under_zero_retry_policy
- test_recorded_apus_both_filled_before_primary_close_is_not_a_waiting_sibling
- test_keeprest_flip_without_close_never_cancels_waiting_buy
- test_recorded_ncpl_consumed_restart_emits_barrier_once
- test_recorded_ncpl_cancel_barrier_selects_only_its_waiting_buy_not_protective_sell
- test_retry_leftover_unknown_cancel_stays_blocking
- test_retry_cancel_receipts_never_retire_owner_or_allow_second_entry
- test_recorded_ncpl_terminal_receipt_requires_matching_positive_broker_proof
- test_retry_cancel_receipts_polled_off_thread_without_removed_wait_flag

41 new test cases; combined RETRYLEFT1/retry-one/CLEARWAIT1/KEEPREST1/RESERVE1/
manual-stop controls:246 passed3.59s. Complete unit run on source215d3853:
7,155 passed /56 failed460.41s; current main994f08ae:7,114 passed /56 failed.
Added/removed failed names both empty. UNIT_PAIR.json contains every exact
failed name and output hashes. Candidate log and JUnit are committed alongside
it; baseline read-only source receipt from today's SLOT/current-main run is
also retained. Imports resolved to this worktree's src, not the primary checkout.

The first naive stdout comparison is preserved as UNIT_PAIR_UNNORMALIZED.json:
an asynchronous RuntimeWarning was appended immediately after baseline's
field_acceptance.py] node id. The existing ownmix1 exact-node parser removes
only trailing stderr; both actual56-name sets match. No test assertion was
changed to obtain that result. CI must independently pass on the published
exact head before review readiness; pin/merge/install are not claimed here.

## Mutations

Each is applied in memory by run_mutation.py, independently, then the41-case
test file runs. No checked-out source is overwritten. All13 are RED:

| Mutation | Failing tests |
| --- | ---: |
| cancel_not_emitted | 30 |
| mirror_barrier_removed | 23 |
| soft_rest_not_disarmed | 8 |
| receipt_grants_entry | 2 |
| receipt_token_ignored | 1 |
| receipt_age_ignored | 1 |
| scanner_proof_reused | 1 |
| retry_allowed_still_cancels | 1 |
| unknown_cancel_accepted | 3 |
| bound_close_proof_removed | 1 |
| terminal_source_ignored | 1 |
| terminal_status_ignored | 1 |
| unknown_terminal_outcome_accepted | 2 |

These are test assertion/missing-output failures, not collection errors or
timeouts. Raw output: mutations/*.txt.
