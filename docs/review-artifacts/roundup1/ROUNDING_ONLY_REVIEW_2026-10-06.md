# ROUNDUP1 #1095 - Rounding-Only Review Receipt

## Verdict And Scope

Ready for independent review, not pinned or installed. Rounding-only card:
format the offset buy price to four decimals, then ceil to the broker tick.
No shared rounding-helper, exit-rule, ORB, offset or band-setting change.
The catalog requires the flag ON at deployment; its ruling no longer says
"not tonight". Settings retains FALSE as the explicit compatibility/rollback
value. The reviewed deploy must set the flag TRUE and verify it on v2 and OMS,
which now rounds an unsent PA1 deferred mirror when it resubmits.

Removed `_fetch_legacy_resting_orders`, `legacy_resting.py`, its generic
recovery tests/mutation script, and the obsolete diagnostic Python probe.
Their historical logs below REPORT.md are evidence, not this PR's runtime.
Two separate findings are recorded in C21/C22 of shared handoff commit
`7bac6d0f372199f222787030ec875e2542d204c5`: unbounded archive recovery and
explicit-target cancels falling back to the latest open symbol order.
The OMS cancel dispatcher is unchanged. No production or main change.

## Exact Test Tree And Full Pair

Frozen source/test commit: `946edf4e3bb6b453861cdb120bd3c391a3bd366d`.
Only docs and human-readable catalog ruling text follow it; expected values
and source/test bytes are unchanged. Application baseline is exact
`7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`. Current main `03b26293`
differs only in three handoff docs; source/tests/ops/scripts are identical.
Same macOS ARM64 Python3.12 virtualenv first on PATH, checkout-local PYTHONPATH.

| Full tests/unit | Failed | Passed | Duration |
| --- | ---: | ---: | ---: |
| Main application baseline | 47 | 5894 | 270.82 s |
| ROUNDUP1 rounding-only | 47 | 6314 | 259.03 s |

**Failed names identical: zero additions/removals, XML-based comparison.**
This is the observed local47 baseline, not the earlier48/56 baseline claimed
as reproduced. All47 identities and XML hashes are in
[failed-name receipt](ROUNDING_ONLY_FAILED_NAMES_2026-10-06.json).
Raw `/tmp/roundup1-scope-main-20261006.log` SHA256
`565195daea1f7e483663e418fbcd95da6a139fe169f937982f848c469ee1afdf`;
head `/tmp/roundup1-rounding-final-unit-20261006.log` SHA256
`9d769377acbef13a4aa1eaabdf3f5b87cbfd84febe82a295124c5beb6a56c390`.

Focused rounding + test-quality control424 PASS (420 ROUNDUP +4 quality);
full Ruff src/tests and diff whitespace PASS. Prior composition focus921
passed before the two PM-reference controls were added; the final full suite
includes those composition tests and all420 ROUNDUP cases.
The intermediate self-derived-expectation lint failure was corrected with a
literal one-cent-changed RPG expectation, not a lint waiver.

## Acceptance And Replays

| Case | Result | Exact control |
| --- | --- | --- |
| SCKT10-05, 12:47-12:57 | stop1.07, limit1.08 both wires; captured max1.06, no trigger | `test_recorded_sckt_strategy_oms_adapter_wire_is_ceiling_not_nearest`, `test_recorded_sckt_1247_1257_tape_never_reaches_new_stop` |
| $5 line | 5.0250 ->5.03 both legs; cent-exact4.51 unchanged | `test_five_dollar_line_has_503_wire_and_cent_exact_is_unchanged` |
| PM recorded MEDS4.6489 print | below ceiling4.65: no cross/latch/slot, both callbacks | `test_recorded_meds_print_below_ceiling_cannot_cross_or_take_slot` |
| PFSA Schwab bracket | sent3.78 -> target3.97 / stop3.48, not raw3.7736 reference | `test_pfsa_bracket_uses_the_single_sent_trigger_not_legacy_exit_reference` |
| NXL PM both legs | trigger7.15; ask-priced LIMIT/reference7.16, original ask sizing84/42 | `test_recorded_nxl_pm_limit_reference_remains_the_price_actually_sent` |
| Restart before/after flag | own pre-flag Webull1.06/1.07,280; post-flag1.07/1.08,278; no new buy | `test_restart_restores_recorded_accepted_wire_not_new_calculation_and_never_duplicates` |
| SCKT RPG reprice first/reclaim, both legs | recomputed1.07/1.08; OMS canonical matches; true cent change distinct | `test_recorded_sckt_reprice_authorization_recomputes_ceiling_and_matches_oms` |
| PA1 deferred SCKT geometry | resubmit ceil1.07/1.08 through serial lane,278 shares, one submit | `test_recorded_sckt_pa1_resubmit_recomputes_ceiling_then_serial_lane_sizes_wire` |

SCKT's retained tape summary is262 prints, maximum1.06, zero at/above1.0636.
The new trigger is never reached in that captured interval. This is NOT a
counterfactual broker-fill or improved-P&L claim. The retained actual280@1.06
buy and280@1.005 sale show -$15.40 before costs. New notional sizing would be
Schwab556 / Webull278 at the1.08 wire limit; no fill is simulated as real.

Retained own census179 orders is not the reviewer's narrower173 population.
Restart and PA1 controls reuse recorded SCKT/PFSA order prices/IDs but explicitly
control accepted/unfilled status, broker-origin proof, clock and eligibility.
No historical ROUNDUP-enabled order or historical PA1 partial fill is claimed.
Restoration uses exact existing RPG bindings; generic no-ticket archive
discovery is excluded by the scope ruling. Missing own-wire proof stays blocked.
Sub-$1 boundaries are synthetic edge controls: no sub-$1 order is in this census.

## Mutations - 14/14 RED

Each is an in-memory single mutation; no production source file is rewritten.
Clean ROUNDUP control420 PASS. Raw `/tmp/roundup1-final-mutation-<name>.log`;
each mutant pytest rc1 (assertion failures), wrapper rc0.

| Mutation name | Protected behavior | Result |
| --- | --- | --- |
| nearest | no nearest-cent buy trigger | RED |
| nearest_schwab_wire | Schwab actual adapter path alone | RED |
| nearest_webull_wire | Webull actual adapter path alone | RED |
| nearest_pm_compare | recorded MEDS cross comparison alone | RED |
| floor | no downward rounding | RED |
| wrong_subdollar_tick | preserve sub-dollar tick | RED |
| raw_limit_sizing | quantity uses final wire limit | RED |
| pair_lift_removed | existing collapsed-band pair protection | RED |
| pm_raw_trigger | quote path must compare rounded trigger | RED |
| restore_recalculates | own recorded wire, not new line calculation | RED |
| legacy_exit_reference | native bracket follows sent stop | RED |
| unproven_restore_releases | own unproven wire remains blocked | RED |
| pa1_copies_nearest | unsent PA1 mirror recomputes ceiling | RED |
| pm_reference_uses_trigger | PM keeps sent LIMIT reference | RED |

AST comparison to baseline confirms `_schwab_round`, `_round_to_tick` and
`resting_wire_limit` bodies byte-equivalent in AST; no global helper edits.
Catalog denominator is140 boolean +8 numeric =148; not an actual live gate run.
Pin/merge and exact-SHA reviewed install remain separate, not implied by tests.
