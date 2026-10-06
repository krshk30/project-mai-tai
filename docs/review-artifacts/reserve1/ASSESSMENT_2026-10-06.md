# RESERVE1: Exact-parent protection at the Schwab sell boundary

## Verdict And Scope

**NOT READY FOR PIN: one hard-stop coverage decision remains.** The current
candidate protects the CW managed hard stop, but not the separate `ArmedHardStop`
sender. Clarification was requested before any review-ready claim or deployment.
The initial over-broad candidate also changed the deliberately fail-open 19:55
flatten policy; this was corrected by limiting the guard to the three requested
managed-send paths. An explicit counterfactual now pins the unchanged exception.

AGREE on the send-boundary defect and the proposed exact-parent release. A fail-open
native-OCO cache may resume the exit ladder, but cannot authorize a Schwab managed
sell. This isolated branch changes OMS only; it does not merge, install, restart a
service, change flags, or write a ledger. Base: `4805ddc81184c76b4d5cef5c483c809edb666fe6`.

The exact APUS cache state and historical complete sync durations are UNMEASURED,
not proven stale. The new telemetry measures those quantities prospectively.

## Independent Read-Only Evidence

Read installed OMS and Schwab adapter source on `mai-tai-vps` at application
`7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`, PostgreSQL order/managed-row records,
`/var/log/project-mai-tai/oms.log`, and exact-parent Schwab GET responses. No token
refresh, service action, database write, or bulk Redis read was performed.
Broker response fields required by the parser are preserved in
`tests/fixtures/reserve1/recorded_orders.json`; account hashes and tokens are omitted.

| Case | Own evidence | Interpretation |
| --- | --- | --- |
| APUS, October 6 19:08 UTC | Generic cancel requested=2, confirmed=0 at 19:08:03.698; close placed at 19:08:03.731; oversold rejected at 19:08:04.992. Parent 1008196163482 FILLED 78; children 1008196163484/85 SELL WORKING under an OCO wrapper on the independent read. | The software send did not require a confirmed exact-parent release. |
| IPDN, October 6 18:03 UTC | Native RELEASED at 18:03:04.275; parent 1008191994240 FILLED 132, both children CANCELED, zero filled on the independent read. | Fresh-note control follows the existing release path. |
| Three retained NXL oversold closes | Broker orders 1008144935878, 1008144935921, 1008145794009: REJECTED, zero filled, two shares each. | The archive is named oms.log-20261002.gz, but broker enteredTime is October 1 at 21:08:06, 21:39:03 and 22:36:55 UTC. They are not October 2 events. |

Today's log denominator is eight flip-close attempts: three Schwab, five Webull.
Only IPDN has a native-release marker among the three Schwab attempts. OLOX was an
extended-hours bare-order case; absence of that marker is not itself a defect.
No recorded expiry marker establishes APUS's note age. An absent note and a stale
note take the same unsafe old branch, and both are exercised separately.

Between 19:05 and 19:08:03 UTC, position-stage stamps were:
19:05:05.857, 17.017, 34.773, 47.101;
19:06:04.727, 18.984, 35.421, 47.029;
19:07:02.551, 18.571, 32.267, 49.201; 19:08:02.742.
Spacing is 11.160-17.756 seconds. These are cadence markers, not complete-pass
start/end pairs. Neither a pass exceeding 30 seconds nor a count of stale notes
at the eight historical evaluations can be recovered from those stamps.

## Implementation

- For flip, confirmation, and CW hard-stop sends, the managed-exit emitter claims
  the exact managed-row episode before a
  broker await. For Schwab, it requires release through the owned entry parent,
  retains the adapter's post-cancel confirmation GET, and rechecks row identity,
  quantity, and an existing close before sending. Unknown evidence refuses the send.
- Fresh flip and confirmation callers reuse their just-completed row-bound release;
  the old 30-second cache remains a ladder decision, not permission to send.
- A child fill resolves through the existing owned-child attribution/close path and
  suppresses the software close. Partial/unknown child state stays fail-closed.
- Oversold recovery requires an explicit broker rejection with zero filled quantity.
  When an order id exists, a fresh read of that exact close must confirm REJECTED/0.
  The Schwab adapter's explicit HTTP refusal without an order id is accepted as a
  refused POST, never a client/transport error. Release is then repeated before one
  replacement close. A child fill, unknown original close, changed quantity, or
  working close prevents it. Recovery is claimed once per episode in this OMS
  process; existing ordinary ladder retry/backoff policy is not replaced.
- Preserve the original rejected attempt before the independent recovery transaction;
  otherwise rollback of that read session can lose the attempt in a shared-connection
  test. Both original and retry client ids remain auditable.
- Existing EOD handover gates and exit backoff run before removing protection.
  Webull's reservation/cancel-then-sell path, generic exit controls, the explicit
  19:55 flatten exception, and native target resolution are unchanged.

Telemetry: `[OMS-V2-CW-FLIP-NOTE]` records fresh/stale/absent plus age at flip and
confirmation evaluations. `[OMS-V2-CW-FLIP-PROTECTION]` records every guarded send
path. `[OMS-BROKER-SYNC-PASS]` logs one start and one end with pass id, outcome,
duration_ms and whether the duration exceeded the existing note-age bound, including
exceptions and cancellation.

## Recorded Replays And Controls

`test_reserve1.py` drives the recorded APUS and IPDN trees through the real Schwab
release parser and the OMS evaluator/send/report paths. Future successful closes,
cancel results, and child-fill races are explicitly counterfactual broker-shaped
responses, not claimed historical outcomes.

| Test | Required outcome |
| --- | --- |
| test_apus_1908_aged_or_absent_note_release_confirm_then_one_close | GET parent, cancel children, GET parent, one 78-share close; absent/stale note and release logged. |
| test_ipdn_1803_fresh_note_control_reuses_release_without_extra_parent_read | One existing release and one close; no duplicated release. |
| test_apus_child_fill_during_cancel_suppresses_software_sell | Owned-row resolution, zero software sells. |
| test_apus_cancel_ack_without_terminal_reread_never_authorizes_close | Successful DELETE with working children still refuses. |
| test_apus_unreadable_parent_never_fails_open_on_send | Unknown parent, zero sells. |
| test_schwab_software_hard_stop_also_releases_exact_parent | Same release-confirm-send boundary without a pending flip. |
| test_schwab_oversold_recovery_requires_rejection_proof_then_release_once | Exact rejected-close proof precedes one recovery release and one retry. |
| test_schwab_original_close_unknown_or_filled_never_retries | Unknown/filled close suppresses recovery. |
| test_schwab_child_fill_during_oversold_recovery_prevents_second_sell | Child fill during recovery, zero second sells. |
| test_schwab_second_oversold_reject_has_no_recursive_retry | One recovery only; second rejection remains recorded. |
| test_schwab_later_evaluation_cannot_repeat_episode_recovery | A later ladder evaluation cannot repeat the episode's recovery. |
| test_schwab_parallel_quotes_never_submit_two_sells_for_one_lot | One submit under concurrent evaluations. |
| test_schwab_quantity_change_during_release_never_sells_stale_lot | Quantity change suppresses the stale close. |
| test_existing_exit_backoff_does_not_strip_native_protection | No broker action during the existing backoff. |
| test_existing_1955_unknown_handover_keeps_explicit_flatten_exception | The recorded APUS shape in a counterfactual policy-boundary test still takes the existing explicitly authorized flatten path. |
| test_nxl_three_retained_oversold_reports_require_exact_rejected_zero_fill | All three recorded rejects require exact-id REJECTED/0; client/partial responses refuse. |
| test_note_state_logging_measures_absent_fresh_stale | Includes the 30-second boundary. |
| test_sync_pass_start_end_duration_survives_errors | Start/end and duration for success, exception, and cancellation. |

Regression controls include test_v2_cw_managed_exit, test_v2_eod_oco_transition,
test_confirmation_exit_fanout, test_oms_native_oco_stand_down,
test_exit_reservation_release, test_v2_managed_exit and test_schwab_exit_only_oco.

## Verification

Final expanded regression set: **333 passed**, including **27 RESERVE1 cases**.
This includes the previously missed retry-ceiling, Webull late-close and OWNMiX
reconciliation controls, without changing their assertions. Ruff and
`git diff --check` pass. Full-unit same-window pair: base **57 failed / 6,468 passed**;
candidate **57 failed / 6,495 passed**; failed-name diff **EMPTY, 57/57 identical**.
The names and raw paths are in `FULL_UNIT_PAIR_2026-10-06.md`. The earlier reviewer
48-failure result is not substituted for this machine's measurement.

The mutation checker at `tests/reserve1_mutation_check.py` runs each mutation in
memory in a separate process, without rewriting tracked production source.
All **14 mutations RED**, each by an actual test assertion failure:

| Mutation | Result |
| --- | --- |
| M1 bypass send-boundary release | RED |
| M2 admit unknown release | RED |
| M3 ignore initial child fill | RED |
| M4 remove concurrent episode claim | RED |
| M5 remove post-release quantity checks | RED |
| M6 omit original rejection proof | RED |
| M7 ignore child fill during recovery | RED |
| M8 remove per-episode recovery cap | RED |
| M9 treat client reject as broker proof | RED |
| M10 admit an original partial fill | RED |
| M11 report every note as fresh | RED |
| M12 omit sync-pass end marker | RED |
| M13 remove the adapter post-cancel GET | RED |
| M14 omit the evaluation note marker | RED |

Verified source SHA256:
`22676a6866321de99332c0d26d126e06f2e664313b425ef231f298ed899b13f2`.
Recorded fixture SHA256:
`577d8a3dbae6eb357c9e67f0eed4d174ed49d545dc46be3fc85e89e0b5f8ff88`.

## Deploy And Rollback Boundary

### Scope Boundary And Outstanding Decision

1. `_v2_overnight_flatten` deliberately passes `allow_unconfirmed_overnight=True`
   and submits at 19:55 despite unconfirmed protection. The first over-broad guard
   changed that behavior. The scoped candidate preserves it and pins one successful
   close through the existing path, with zero extra parent reads. No new overnight
   policy is inferred from this fix; changing it requires an explicit ruling.
2. `_evaluate_hard_stop_market_event` calls `_trigger_hard_stop`, which sends through
   `process_trade_intent`, not `_emit_v2_exit_on_loop`. Its RTH native-stop check is
   deliberately fail-open on an exception. The current CW hard-stop replay does not
   prove that separate path safe. Decide whether the requested software-hard-stop
   coverage includes it; if yes it needs a separate exact-parent send-boundary
   integration and recorded-path test before pin.

The first full-suite candidate had 79 failures versus 56 on the clean base. The
22 new exit-control failures were traced and reproduced: unnecessary database
reads in untouched emitters, and interception of generic owned-fill reconciliation.
The narrower candidate passes all 22 controls without altering their assertions.
The other extra failure, ROUNDUP1's SCKT deferred replay, also fails on unchanged
base after the session cutoff (`resting_window_ended`); a same-window full pair is
was measured rather than counting that clock dependency as a new source defect;
both completed runs now have the identical 57 failed names.

This is an OMS code fix with no new flag, schema migration, gateway change, or
subscription protocol change. A future reviewed deploy needs OMS restarted onto
the exact approved application SHA. No install is performed by this branch.
Rollback is a reviewed code rollback plus OMS restart; neither is pre-authorized.
Broker latency and tomorrow's note-age distribution remain live measurements,
not replay evidence.
The three NXL cases pin rejection-proof handling from the exact recorded reports;
their historical parent-tree release and successful retry outcomes are not retained
in these fixtures and are UNMEASURED, not claimed as historical fills.
