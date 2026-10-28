# Session Handoff - October 27, 2026

Updated 2026-10-27 18:19 ET for CI review; production observations below remain bounded to their original times, last read 16:48:08 ET. This section supersedes the historical October 16 snapshot below.
Authority: CLAUDE-1 LATEST C11629/C11634/C11635/C11802/C11813/C11821/C11832; no independent production work.

> The 10-20 install state is in git history of this file (main 40f845eb).

## Evening 19:13–20:15 ET (claude-1, session restarted 19:13) — Group-1 status

Operator plan 18:5x: Group-1 = protection (A1 WBEXIT1 D1, A2 SAIQ never-protected, A3 preflight) + missed trades (B1 NUWEREF1, B2 WFF, B3 fade<30%, B4 IPST) + D5 ATR page. **Every fix installs with its switch OFF, except the ATR page (live).** No install without the operator's GO.

| Lane | State | Evidence / next |
|---|---|---|
| A1 WBEXIT1 D1 | FIX FORWARD (2 P1s when ON: read-fail after release; commit join > 9 s) | design 19:59 + sizing amendment (re-protect qty = released − our sell's fills from order status; unreadable → no order + PAGE); target 21:15 |
| A2 SAIQ | 10-20 CLOSED (un-quantized price compare, fixed by WBATTACH3 #1167, confirmed by both); 10-22 strict-owner UNKNOWN → closed as watch | 8 Webull bare fills / 2 never-protected 10-20→10-27 |
| A3 preflight psql-as-flat | ACCEPTED (set -u + explicit psql checks; 8/8 + mutation) | rides Group-1 |
| B1 NUWEREF1 | BUILD (switch OFF), target 21:45 | 10 sessions: 1 proven ref-gap (NUWE) / 52, 39 by design, 12 unknown; NUWE NEW confirms 11:49 ET |
| B2 WFF | CLOSED by design | 7 live symbol-days: 4 earlier real orders, 3 late-admission seed caps (09-16 rule); 0 post-SELL restarts |
| B3 fade<30% | CLOSED by design (06-10 rule) | removed names return; later-move lower bounds = study line for the operator |
| B4 IPST | CLOSED, UNKNOWN → watch | original request not retained |
| D5 ATR page | REVIEWED (codex AGREE 8468908a) | date filter default today ET, descriptions, REAL badge on Recorded LIVE table (operator 19:3x) |

Group-1 local composition (A3 + D5): 7322ae0b, unpushed, uninstalled. PR #1168 required CI re-running (head 2c3ad3ee). Box 20:0x: L12 complete=True accounts=2 steady, 0 NATIVE-FAIL since 16:36, 9/9 services NRestarts 0. Found: prune-capture.service failed Sun 10-25 05:34 ET (lock timeout, nothing lost; next run Sun 11-01).


## Installed And Verified

- Box checkout: `20dc337e73df56c588f928ced6a95060a457cd23`.
- Installed tree: `4f3cdedb55883057117aec8bd407d0639b04b0dd`.
- Source tree: `2f651941577e1f10cf46514383f6cd44d40bfac1`.
- Base V4: `31f108ee665e0c7bf4ca4fed9d0a6ffaab476b76`, tree `fdf18b833b9b07dafa09372ac43a2372b36e6028`.
- L12 successors: `48722bc7` empty native-book decoding; `77315e48` sanitized failure logging (includes `13f8b292`); `86b32d5a` bounded UNKNOWN logs and refusal codes; `20dc337e` explicit L12 background self-interval 30 -> 28 seconds.
- Ordinary 2-second window, foreground/pending/terminal/backoff, actual claim stamps, 30-second refresh-start cadence and alerts remain unchanged. The 28-second guard IS an explicitly authorized policy relaxation, not a measured vendor quota or rate-equivalence result.
- Latest OMS-only restart: 16:36:37.353840001..16:36:38.473348018 ET; PID `2618307`, NRestarts 0. Other ten unit identities were unchanged. Other running services were not reloaded for these L12 changes.
- OMS health gate passed 16:36:56.776570 ET. End check 16:48:08 ET saw healthy heartbeat 16:47:58.827948 ET, age 9.67 seconds; clean source and unchanged env.
- Related tests on exact final head: 41 PASS, 1.33 seconds, all 3,427 tracked pins unchanged. No full suite or performance qualification.
- Fixed 16:36:38..16:46:38 ET observation: 18/18 terminal publications complete=True accounts=2; zero native failures or refusal conditions; zero literal OMS ERROR/CRITICAL/Traceback/never-awaited/destroyed-task lines. All eleven end identities matched post-restart state.
- UNKNOWN details: 45 once, then zero repeats; each of 18 summaries still says 45 symbols / 973 uncertain local rows. These L12-scoped holds are NOT cleared by native completeness.

## Environment And Database Changes

Exactly three V4 env additions, each once; no later L12 env change:
`KEEPWATCH1_ENABLED=true`, `BROKERDECIDES1_ENABLED=true`, `BROKERDECIDES1_MAX_AGE_MS=20000`.
Env SHA256: `827f9239175e2491e3292ba95ddff5d716c335c45514dd7bcf8acefa6a0263f2`.

- Migration file `20261022_l12_dispatch_retirement`; actual installed revision `20261022_l12_retirement`, previous/down revision `20261023_atr_aggregate`.
- Added `brokerdecides_dispatch_evidence` and `brokerdecides_dispatch_retirements`; four indexes: retirement token, control uncertainty, dispatch scope, order dispatch token. Actual total migration elapsed 2.869640889 seconds within approved 180 seconds; individual index durations UNKNOWN.
- YMAT ONLY: intent `d17764df` set cancelled with reason `l11_install_retire_stale_pre_1027`; linked order `2af0c331` was already aborted and unchanged. AIXI was NOT retired.
- Mirror4 ONLY: DRMA/JYD/TDIC/JZ canonical live:orb rows committed; four linked orders unchanged and other seventy typed-row digest unchanged. No later row settlement.
- IPST one order + one parent settlement at 11:12 was separately accepted; do not repeat it or its restart/watch.

## Scoped Pre-Gate Exceptions And Operating Rules

- C1/operator authorized install before 16:05 and before CI/merge, detached exact candidate/tree. This did not waive flatness or later source/review gates.
- Named initial two-intent/reconciler findings had specific C1 dispositions, not blanket all-green or permission to ignore new failures.
- Native pre-gate UNKNOWN used the explicitly approved completed OMS sync-pass <=60-second method plus both-account fresh SQL flat/zero-working/zero-managed assertions. No forced liquidation or STOP clearing.
- Migration's zero-app-client gate had the named read-only idle trade-tape exception: PG PID2436033 / Python2435695, no touched-table locks, rechecked before DDL; never killed.
- Named quiescent v2 clock disposition and exact Git permission repairs were scoped exceptions, not permanent safety bypasses.
- **Every Git command on the box runs as `sudo -u trader git ...`, never root**, including status/rev-parse. Status must EXIT0 and be empty. C11605 authorized one `.git`-only ownership restoration, before list retained / after non-trader count 0; no recursive worktree repair authority.
- Nine services running at last identity verification; guard failed (NRestarts3) and paper inactive deliberately unchanged by C1 ruling. Not an all-eleven-green fleet. Last retained guard timer was October28 03:40 ET; do not reset/start/rearm from this note.

## Evidence And Remaining Work

- V4: `/var/tmp/oct27-install-v4-20261027-1507` on the box: migration/readbacks, typed preimages, YMAT/Mirror commits, writer exclusion, original 608-second J watch and preserved failures.
- Final tests: `/private/tmp/OCT27-C11634-L12-20DC-RELATED-ONE`; XML `b739d856fce2495e00b89849d05186f065437aa12097bcd42b71c11b1d1aea48`.
- Final runtime: `/var/tmp/oct27-l12-20dc-install-20261027-1636`; raw `8f2bb76bb9b8d646d65ff237fbeed51b7cf951e3acad801b15aa6442311d3b0b`, summary `74bfe9999ffa79c9dc6fe8f93c953e18d45fb0d68a3e3ec6195d620d9372874a`.
- Original native shape failures and all later interval refusals remain immutable. Original 606/180/83/48 and L12 122/34/manual20/FK/boot/schema/indexed-rate obligations, native/economics UNKNOWN, and v2 watched-name/boot-hold/GC-release proof remain explicitly unclosed where unproved. No stale GoalUI completion.
- Reconciler retained findings (five / two critical at latest bounded review) are not erased by the clean OMS window. No extra provider read or production cleanup is authorized here.
- PR1168 required CI FAILED: runs `40579469080` and `40579478304`, completed 17:54:04/11 ET; each 170 failed / 67 errors / 20,119 passed. PR remains unmerged on exact `20dc337e`. C11802/C11813 require classification and C1 disposition first: no fixes, fixture edits, reruns or merge bypass. Main baseline `39551578595` is older than installed `9c10a7de`; no identical-baseline PASS inference.
- Complete 237-ID evidence table: `/Users/velkris/.codex/task-queues/analysis/claude1-reviews/OCT27-C11802-PR1168-CI-CLASSIFICATION.md`, SHA256 `3e43bbeb1cc2fdd3932d7c0fca7f79c3a42160fb9055f7ac5f7319963c119d8c`. A0 / B97 / C74 / D66; C denotes source-linked differences, not 74 unsafe live defects. Four C11813-authorized priority-file comparisons on literal 9c ended 18:17:07 ET: expected-flags 70 PASS plus one different count failure; three collection errors for absent newer modules/helpers. No repair/retry; 3,148 source/test snapshot pins unchanged. Current PR runtime has not been re-tested by those base-only comparisons.
- C11821 accepted A0/B97/C74/D66 and authorized test-only B/C cleanup; C11832 separately authorized exactly one operational `ops/health/expected_flags.json` L12 entry. No application source or migration changes; no D66 fixes. One combined PR commit and required CI cycle only; new counts pending, not a green promise. D-only red still needs the operator's merge decision, no administrative bypass.
- C11832 explicitly replaced full-root-tree equality with equality of merged `HEAD:src` and actual migration tree `HEAD:sql/migrations` against installed20dc. Required installed subtree identities are src `2f651941577e1f10cf46514383f6cd44d40bfac1` and migrations `9f1d87ed0e652f7bc17e90a289298a84fd84bb64`. The reviewed non-src delta is listed separately; no production checkout/restart merely to align a squash SHA.
- **Box non-src sync NOT tonight:** box `ops/health/expected_flags.json` lacks the L12 flag entry until next checkout; the expected-flags audit on the box may report it until then. Tests and the one catalog-entry delta ride the next normal checkout, without a restart for those files. No new checkout/deployment authority follows from this note.
- This existing primary-checkout handoff is outside the frozen PR tree, so documenting deployment does not change the reviewed/installed tree. Unrelated working-tree changes are untouched.
- **HOLD** D1, stale-row cleanup (including 45-name uncertainty and POM/SAIQ/AIOK/NXL/MI), NUWEREF1, ATR page work, momlive-sec and all other lanes until explicit C1/operator decisions. Keep owners and resume points. No idle-time work, alert mute, full suite or further restart from this note.

## C11821/C11832/C11835 Reviewed Non-Source Delta

ONE cleanup commit `2c3ad3ee52d1462a1da1fe62e248fa9a020a5142`, committed18:37:43 ET and fast-forward pushed to PR1168 at18:38 ET, parent20dc. Exactly13 files /99 additions /33 deletions. Source subtree2f651941577e1f10cf46514383f6cd44d40bfac1 and migration subtree9f1d87ed0e652f7bc17e90a289298a84fd84bb64 unchanged; full tree2210b55acc2cec81c641bb88e974ffe725bccf95 intentionally differs only by reviewed non-src content. No app/sql/tools/workflow changes, no test cases removed, no D67 repairs.

One push launched the normal push/PR Validate pair40596290830 and40596296982 at18:38 ET; no manual CI rerun/workflow dispatch. Initial checks IN_PROGRESS; new pass/fail/skip counts UNKNOWN until completion, not promised green. Original170F67E CI remains historical failure. No box checkout/restart/config sync tonight.

Exact reviewed file membership:

- `ops/health/expected_flags.json`
- `tests/unit/test_falseflip2_release.py`
- `tests/unit/test_keepwatch_session_producer_chart_oracle.py`
- `tests/unit/test_l12_friday18_actual_packet_contract.py`
- `tests/unit/test_l12_main_nonce_target.py`
- `tests/unit/test_l12_nonce_restore_caller.py`
- `tests/unit/test_l12_three_index_statement_budget.py`
- `tests/unit/test_l14_only_a2_fallback_composition.py`
- `tests/unit/test_momcancel1_ack.py`
- `tests/unit/test_momneg1_signal_native_rate.py`
- `tests/unit/test_no_inert_modules.py`
- `tests/unit/test_price1_recorded_gixi_db_contract.py`
- `tests/unit/test_v2_boot_gc_freeze.py`

## C11821/C11835 Operator List: D67 Known Failures

C11835 adds the residual `tests/unit/test_no_inert_modules.py::test_no_new_module_is_imported_by_nothing` to the original D66 list below: only the newly reviewed install_chunks inventory entry is recognized; the remaining36 module paths already exist on installed9c and are NOT allowlisted. D67 is the expected remaining known-failure membership, not a claim that a new CI run has already finished. Original237-ID classification/CI raw outcomes stay immutable.

Exact residual module paths (source membership on9c verified; no assertion that they were runtime-tested there):

- `src/project_mai_tai/atr_simulation/aggregate_entry_contract.py`
- `src/project_mai_tai/atr_simulation/aggregate_entry_publication.py`
- `src/project_mai_tai/atr_simulation/aggregate_entry_reader.py`
- `src/project_mai_tai/atr_simulation/cohort_assembler.py`
- `src/project_mai_tai/atr_simulation/cohort_provenance.py`
- `src/project_mai_tai/atr_simulation/cohort_sources.py`
- `src/project_mai_tai/atr_simulation/engine.py`
- `src/project_mai_tai/atr_simulation/entry.py`
- `src/project_mai_tai/atr_simulation/execution_eligibility.py`
- `src/project_mai_tai/atr_simulation/exits.py`
- `src/project_mai_tai/atr_simulation/fill_models.py`
- `src/project_mai_tai/atr_simulation/firstbar.py`
- `src/project_mai_tai/atr_simulation/firstbar_binding.py`
- `src/project_mai_tai/atr_simulation/firstbar_comparison.py`
- `src/project_mai_tai/atr_simulation/firstbar_exits.py`
- `src/project_mai_tai/atr_simulation/firstbar_live_baseline.py`
- `src/project_mai_tai/atr_simulation/firstbar_reference.py`
- `src/project_mai_tai/atr_simulation/firstbar_replay.py`
- `src/project_mai_tai/atr_simulation/firstbar_study.py`
- `src/project_mai_tai/atr_simulation/firstbar_study_publication.py`
- `src/project_mai_tai/atr_simulation/firstbar_target_rule.py`
- `src/project_mai_tai/atr_simulation/models.py`
- `src/project_mai_tai/atr_simulation/policy_io.py`
- `src/project_mai_tai/atr_simulation/read_model.py`
- `src/project_mai_tai/atr_simulation/store_schema_20261023_aggregate.py`
- `src/project_mai_tai/atr_simulation/types.py`
- `src/project_mai_tai/atr_simulation/volume_body.py`
- `src/project_mai_tai/atr_simulation/worker.py`
- `src/project_mai_tai/momentum_paper/case_collision_append.py`
- `src/project_mai_tai/oms/bot_control_cleanup.py`
- `src/project_mai_tai/oms/webull_identity_copy_audit.py`
- `src/project_mai_tai/phone_alert_policy.py`
- `src/project_mai_tai/reconciliation/operator_closed_exits.py`
- `src/project_mai_tai/services/atr_simulation_firstbar_worker.py`
- `src/project_mai_tai/services/market_data_snapshot_worker.py`
- `src/project_mai_tai/services/momentum_live_replay.py`

Original D66 test IDs and evidence bounds:

C1 18:21 accepted these as "known failures on the installed base 9c (pre-existing, untested gap since 10-21)". They are NOT fixed tonight. This is the operator classification, not a claim all66 were executed on9c: only four authorized priority-file attempts were run;45 implicated source paths/functions were statically identical9c and three files could not collect against missing newer modules/helpers. Original CI assertions/errors remain failures; the complete per-ID evidence and needed resolution are in the237-ID table.

Test-only B/C cleanup and one new required-CI cycle are authorized; any remaining D-only red CI still awaits the operator merge decision. No administrative bypass or production action follows from this list.

- `tests/unit/test_alerts1_saiq_operator_closed.py::test_real_checker_phone_route_manual_excluded_but_real_or_unknown_miss_pages[True-False-0-0]`: ModuleNotFoundError: No module named 'requests'
- `tests/unit/test_alerts1_saiq_operator_closed.py::test_real_checker_phone_route_manual_excluded_but_real_or_unknown_miss_pages[False-False-2-1]`: ModuleNotFoundError: No module named 'requests'
- `tests/unit/test_alerts1_saiq_operator_closed.py::test_real_checker_phone_route_manual_excluded_but_real_or_unknown_miss_pages[False-True-2-1]`: ModuleNotFoundError: No module named 'requests'
- `tests/unit/test_alerts1_saiq_operator_closed.py::test_audit_commit_failure_does_not_subtract_or_silence_phone`: ModuleNotFoundError: No module named 'requests'
- `tests/unit/test_broker_order_event_source.py::test_webull_exception_paths_are_DERIVED_never_guessed`: AssertionError: only the four pre-flight guards are literal-client: missing account config, invalid raw BUY stop-limit relationship, missing instrument id, and the fresh-market resting-mirror shape guard
- `tests/unit/test_d3_confirmation_projection.py::test_original_three_query_fixture_does_not_hydrate_entities`: AssertionError: assert (9 == 3)
- `tests/unit/test_d3_confirmation_projection.py::test_scalar_projection_preserves_positive_and_identity_refusals[none]`: AssertionError: assert [<class 'proj...ualPosition'>] == []
- `tests/unit/test_d3_confirmation_projection.py::test_scalar_projection_preserves_positive_and_identity_refusals[managed_open_zero]`: AssertionError: assert [<class 'proj...ualPosition'>] == []
- `tests/unit/test_d3_confirmation_projection.py::test_scalar_projection_preserves_positive_and_identity_refusals[managed_unknown]`: AssertionError: assert [<class 'proj...ualPosition'>] == []
- `tests/unit/test_d3_confirmation_projection.py::test_scalar_projection_preserves_positive_and_identity_refusals[vp_positive]`: AssertionError: assert [<class 'proj...ualPosition'>] == []
- `tests/unit/test_d3_confirmation_projection.py::test_scalar_projection_preserves_positive_and_identity_refusals[vp_missing]`: AssertionError: assert [<class 'proj...ualPosition'>] == []
- `tests/unit/test_d3_confirmation_projection.py::test_scalar_projection_preserves_positive_and_identity_refusals[managed_coid]`: AssertionError: assert [<class 'proj...ualPosition'>] == []
- `tests/unit/test_d3_confirmation_projection.py::test_scalar_projection_preserves_positive_and_identity_refusals[fill_quantity]`: AssertionError: assert [<class 'proj...ualPosition'>] == []
- `tests/unit/test_d3_confirmation_projection.py::test_scalar_projection_preserves_positive_and_identity_refusals[fill_native_id]`: AssertionError: assert [<class 'proj...ualPosition'>] == []
- `tests/unit/test_d3_confirmation_projection.py::test_scalar_projection_preserves_positive_and_identity_refusals[order_native_id]`: AssertionError: assert [<class 'proj...ualPosition'>] == []
- `tests/unit/test_d3_confirmation_projection.py::test_scalar_projection_preserves_positive_and_identity_refusals[provider]`: AssertionError: assert [<class 'proj...ualPosition'>] == []
- `tests/unit/test_d3_confirmation_projection.py::test_projection_reads_current_books_each_time_without_result_cache`: AssertionError: assert (6 == 6 and 6 == 3)
- `tests/unit/test_d3_held_working_buy_reads.py::test_original_held_and_unknown_books_keep_three_selects`: AssertionError: assert 9 == 3
- `tests/unit/test_d3_held_working_buy_reads.py::test_mixed_books_read_working_buys_only_for_potential_flat_symbol`: AssertionError: assert ['DHELD', 'DFLAT', 'DUNKNOWN'] == ['DFLAT']
- `tests/unit/test_keepwatch_boot_exclusion_recovery.py::test_timer_clear_uses_real_repaired_prefix_then_original_fleet_and_dangerous_check`: assert not True
- `tests/unit/test_keepwatch_boot_exclusion_recovery.py::test_clear_after_fleet_latch_does_not_relatch_or_touch_economic_state`: assert not True
- `tests/unit/test_keepwatch_boot_exclusion_recovery.py::test_timer_proof_revalidation_adds_no_provider_or_database_operation`: AttributeError: None has no attribute 'fetch_session_history'
- `tests/unit/test_l12_boot_two_owner_delegation.py::test_actual_two_owners_park_and_delegate_both_at_shared_deadline`: AssertionError: assert [] == ['AAA', 'BBB']
- `tests/unit/test_l12_boot_two_owner_delegation.py::test_same_clock_membership_step_precedes_owner_retry_without_skipping_it`: AssertionError: assert (0 == 2)
- `tests/unit/test_l12_boot_two_owner_delegation.py::test_one_symbol_event_wake_does_not_broadcast_to_sibling`: AssertionError: assert [] == ['BBB']
- `tests/unit/test_l7_orb_webull_tickcap.py::test_provider_legal_stop_and_cap_boundaries[.99999-1.00-1.01]`: AssertionError: assert Decimal('1.00') == Decimal('1.01')
- `tests/unit/test_momage1_source_age.py::test_shared_sixteen_cap_and_eligibility_unchanged`: AssertionError: assert ('observed_warning' == 'warning_symb...r_time_filter'
- `tests/unit/test_momlive1_refusal_logs_c8.py::test_no_wire_refusal_keeps_actual_reason_and_quote_inputs[timeout]`: AssertionError: 
- `tests/unit/test_momlive1_refusal_logs_c8.py::test_no_wire_refusal_keeps_actual_reason_and_quote_inputs[quote_missing_future_or_stale]`: AssertionError: 
- `tests/unit/test_momlive1_refusal_logs_c8.py::test_no_wire_refusal_keeps_actual_reason_and_quote_inputs[rounded_ask_above_cap]`: AssertionError: 
- `tests/unit/test_momlive1_refusal_logs_c8.py::test_no_wire_refusal_keeps_actual_reason_and_quote_inputs[webull_subdollar_quantity_below_100]`: AssertionError: 
- `tests/unit/test_mompage1.py::test_mompage1_recorded_fourteen_trades_survive_stop_and_reload[momentum_30s-/bot/momentum-30-5-31.8044-False]`: StopIteration
- `tests/unit/test_mompage1.py::test_mompage1_recorded_fourteen_trades_survive_stop_and_reload[momentum_30s-/bot/momentum-30-5-31.8044-True]`: StopIteration
- `tests/unit/test_mompage1.py::test_mompage1_recorded_fourteen_trades_survive_stop_and_reload[momentum_60s-/bot/momentum-60-9-175.9955-False]`: StopIteration
- `tests/unit/test_mompage1.py::test_mompage1_recorded_fourteen_trades_survive_stop_and_reload[momentum_60s-/bot/momentum-60-9-175.9955-True]`: StopIteration
- `tests/unit/test_mompage3.py::test_live_note_plain_words_actual_flag_and_no_vwap[True-/bot/momentum-30]`: assert '<strong>Status:</strong> LIVE</div>' in '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\...ed actions in the available warning history</td></tr></tbody></table></div></section>\n    </main></div></body></html>'
- `tests/unit/test_mompage3.py::test_live_note_plain_words_actual_flag_and_no_vwap[True-/bot/momentum-60]`: assert '<strong>Status:</strong> LIVE</div>' in '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\...ed actions in the available warning history</td></tr></tbody></table></div></section>\n    </main></div></body></html>'
- `tests/unit/test_mompage3.py::test_live_note_plain_words_actual_flag_and_no_vwap[False-/bot/momentum-30]`: assert '<strong>Status:</strong> OFF</div>' in '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\...ed actions in the available warning history</td></tr></tbody></table></div></section>\n    </main></div></body></html>'
- `tests/unit/test_mompage3.py::test_live_note_plain_words_actual_flag_and_no_vwap[False-/bot/momentum-60]`: assert '<strong>Status:</strong> OFF</div>' in '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\...ed actions in the available warning history</td></tr></tbody></table></div></section>\n    </main></div></body></html>'
- `tests/unit/test_momtpc_audit.py::test_same_day_prepare_uses_corrected_saved_cache_without_API`: assert 0 == 1
- `tests/unit/test_momwarn1_l3_contract.py::test_actual_snapshot_lifecycle_handler_does_not_borrow_warning_epoch_or_print_identity[FILLED-fill-entry]`: AttributeError: 'types.SimpleNamespace' object has no attribute 'strategy_windows'
- `tests/unit/test_momwarn1_l3_contract.py::test_actual_snapshot_lifecycle_handler_does_not_borrow_warning_epoch_or_print_identity[EXITED-exit-exit]`: AttributeError: 'types.SimpleNamespace' object has no attribute 'strategy_windows'
- `tests/unit/test_momwarn1_l3_contract.py::test_queued_old_snapshot_after_new_warning_epoch_retains_original_envelope_only`: AttributeError: 'types.SimpleNamespace' object has no attribute 'strategy_windows'
- `tests/unit/test_oms_schwab_policy_reject.py::test_polled_pmax_policy_reject_is_logged_cached_paged_and_blocks_retry`: IndexError: list index out of range
- `tests/unit/test_orb_pastlevel.py::test_tick_boundary_and_nominal_upward_rounding[.50123-.5013-.5038]`: ValueError: PRICE1-REFUSED
- `tests/unit/test_orb_pastlevel.py::test_tick_boundary_and_nominal_upward_rounding[.99999-1.0000-1.01]`: ValueError: PRICE1-REFUSED
- `tests/unit/test_orb_schwab_bracket_plan.py::test_schwab_adapter_never_submits_orb_as_a_naked_single_leg`: AssertionError: assert 'native STOP_LIMIT bracket' in 'PRICE1-REFUSED'
- `tests/unit/test_orbpast_fresh_ask_applicability.py::test_legacy_missing_or_stale_uses_same_pure_tick_cap[None-.50123-.5013-.5038]`: ValueError: PRICE1-REFUSED
- `tests/unit/test_orbpast_fresh_ask_applicability.py::test_legacy_missing_or_stale_uses_same_pure_tick_cap[None-.99999-1.0000-1.01]`: ValueError: PRICE1-REFUSED
- `tests/unit/test_orbpast_fresh_ask_applicability.py::test_legacy_missing_or_stale_uses_same_pure_tick_cap[2001-.50123-.5013-.5038]`: ValueError: PRICE1-REFUSED
- `tests/unit/test_orbpast_fresh_ask_applicability.py::test_legacy_missing_or_stale_uses_same_pure_tick_cap[2001-.99999-1.0000-1.01]`: ValueError: PRICE1-REFUSED
- `tests/unit/test_orbpast_source_regressions.py::test_new_orb_selection_cannot_drop_fence_to_use_legacy_contract[LIMIT-None]`: AssertionError: assert 'fenced LIMIT bracket' in 'PRICE1-REFUSED'
- `tests/unit/test_orbpast_source_regressions.py::test_new_orb_selection_cannot_drop_fence_to_use_legacy_contract[STOP_LIMIT-ORBPASTLEVEL1-v1]`: AssertionError: assert 'fenced LIMIT bracket' in 'PRICE1-REFUSED'
- `tests/unit/test_orbpast_source_regressions.py::test_new_orb_selection_cannot_drop_fence_to_use_legacy_contract[STOP_LIMIT-unknown-policy]`: AssertionError: assert 'fenced LIMIT bracket' in 'PRICE1-REFUSED'
- `tests/unit/test_pmprint1_pmflip1.py::test_t1_t2_t3_t9_recorded_stray_cannot_take_state[False-WHLR-2026-09-23T13:14:35.011Z]`: AssertionError: assert 'V2-PM-CROSS-BLOCK' in 'INFO     price1:buy_price_floor.py:87 [PRICE1-REFUSED] bot=schwab_1m_v2 symbol=WHLR price=0\n'
- `tests/unit/test_pmprint1_pmflip1.py::test_t10_flag_off_reproduces_saiq_false_buy_and_zero_ask_webull`: AssertionError: assert [TradeIntentD...pct': '0.5'})] == []
- `tests/unit/test_pmprint1_tonight_flags.py::test_s6_catalog_matches_all_on_live_set_and_149_checks_with_restoration_on`: ValueError: not enough values to unpack (expected 1, got 0)
- `tests/unit/test_pmrest1.py::test_p10_cross_drafts_match_unchanged_rest_with_600_300_sizing[True]`: AssertionError: assert [TradeIntentD...pct': '0.0'})] == []
- `tests/unit/test_price1_pm_stream_validated_ask.py::test_raw_subfloor_line_cannot_be_rescued_by_valid_ask_or_cap`: AssertionError: assert TradeIntentDraft(symbol='CLRO', side='buy', intent_type='open', quantity=Decimal('594'), reason='schwab_1m_v2 ATR Flip... '1.0100', 'entry_computed_shares': '594', 'eh_cross_source': 'stream_trade', 'eh_cross_print_ts_ms': '1790594699992'}) is None
- `tests/unit/test_roundup1.py::test_subdollar_default_dollars_still_meet_webull_hundred_share_minimum`: ValueError: not enough values to unpack (expected 1, got 0)
- `tests/unit/test_t43_one_leg_recovery.py::test_t43_ordinary_open_local_abort_is_terminal_only_for_its_new_audited_intent[True]`: IndexError: list index out of range
- `tests/unit/test_t43_one_leg_recovery.py::test_t43_ordinary_open_local_abort_is_terminal_only_for_its_new_audited_intent[False]`: IndexError: list index out of range
- `tests/unit/test_t43_one_leg_recovery.py::test_t43_ordinary_open_local_abort_is_terminal_only_for_its_new_audited_intent[no_event_id]`: IndexError: list index out of range
- `tests/unit/test_v2_entry_sizing.py::test_v2_dollar_open_refuses_unconfigured_account_before_broker`: IndexError: list index out of range
- `tests/unit/test_webull_native_bracket.py::test_submit_places_a_v3_combo_when_flag_on`: AssertionError: assert (1 == 1 and 'rejected' == 'accepted'
- `tests/unit/test_webull_native_bracket.py::test_submit_stays_single_leg_when_flag_off`: AssertionError: assert (1 == 1 and 'rejected' == 'accepted'

## Historical October 16 Snapshot - Not Current Evidence

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.
> Previous day notes (10-02 → 10-09) are in git history of this file (main 7a845576 and earlier).

## PRODUCTION NOW — as of Fri 2026-10-16 23:4x ET (claude-1 close-out read of the box)

| Item | State now | Evidence (as-of) |
|---|---|---|
| Box checkout | `9df73a18` (branch `install-9df-20261016`): Group D (D1, D2 resting/ATR-BUY, D3, D4, D5) + MOMGUARDEND1 next-batch + SYNC2X hygiene on main `7a845576` | `git rev-parse` 21:4x |
| Installed today | 16:01 efbfa397 (KILLSW1 core, COUNTRYROUTE-ATR, WEEKMAINT1 units) · 18:47 db73a581 (WBATTACH1 + O-1015-14 + ORBROUTE1) · 19:28 DB `jit=off` (SYNC2X) · 20:00 a33a30c2 (MOMCANCEL1) · 21:06 MOMGUARDEND1 guard-only · **23:3x Group D + guard ExecCondition + SYNC2X hygiene (9df73a18)**: oms/v2/reconciler restarted; guard 4 artifacts + unit ExecCondition + manifest + daemon-reload; native off-hours start → `exec-condition` skip, inactive, no OnFailure, no paper | install records in claude-1 memory `project_mai_tai_live_rule_cards.md` |
| Services | oms (since 19:56), schwab-1m-v2 / orb-schwab / control (18:47), strategy (10-13 20:29), market-data (10-14 12:31), reconciler (10-13 19:47), market-capture (10-03). Momentum guard + paper **inactive until Mon 03:40 ET timer** (by design, session closed) | `systemctl` 21:4x |
| Switches (OMS `/proc`) | `BOT_CONTROL_ENABLED=true`, `STRATEGY_SCHWAB_1M_V2_COUNTRY_ROUTE_ENABLED=true`, `ORB_COUNTRY_ROUTING_ENABLED=true`, `ORB_WEBULL_ACCOUNT_NAME=live:orb`, `MOMLIVE_SEC_APPROVED_HEAD=a33a30c2…` | `/proc/<pid>/environ` 21:4x |
| Database | alembic `20261015_bot_control`; **`ALTER DATABASE project_mai_tai SET jit = off`** (required: the ORB reconcile query cost ~1.08M → JIT 744 ms per sync pass; rollback `ALTER DATABASE … RESET jit`) | 19:28 |
| Positions / orders | Flat both brokers; 0 working orders; 0 open managed rows; 0 nonzero VP | gates 21:4x |
| Loop numbers | OMS broker-sync pass p50 ≈ 0.45–0.57 s (baseline 0.525 s) after jit=off; reconciler cycle excess p50 65 ms / p90 91 ms (baseline before Group D) | 19:3x / 20:46 |
| Timers next | Sat 01:00 ET momlive-sec (SEC weekly) · Sat 05:00 prune-ticks · Sun prune-capture/retention · **Mon 03:40 ET momentum guard** · Mon 06:20 preopen | `systemctl list-timers` 21:4x |

## TO BE EXERCISED (installed, waiting for the first real event — read these first on Monday)

| Fix | First live event | What proves it |
|---|---|---|
| MOMCANCEL1 (O-1016-10) | next Schwab momentum buy resting > 2 s | `cancel_wire_sent_coid` set within ~2 s; no `cancel_entry_broker_identity_UNKNOWN`; `[MOMCANCEL1]` CRITICAL only if Schwab ACK id unknown |
| MOMGUARDEND1 (O-1016-12) | Mon 03:40 ET start; Mon 18:10 ET close | 03:40: guard + paper active, `live_handoff=True`; 18:10: STOPPING + exit 0, no watchdog core, unit inactive (not failed) |
| WBATTACH1 + O-1015-14 | first Webull bare fill / first managed software sell | attach within first try; prepared sell row durable before wire |
| ORBROUTE1 (ORB country routing ON) | first ORB entry 09:2x | US name → Schwab, non-US/UNKNOWN → Webull; ORB protection reconcile quiet |
| KILLSW1 core + COUNTRYROUTE-ATR | first ATR entry Monday | route decision logged; no stale-row refusals |
| SYNC2X (jit=off) | every sync pass | p50 ≈ baseline; other services pick up jit=off on next restart |

## INSTALLED LATE 10-16 (23:3x) — Group D, verify Monday

| Item | What changed | Monday proof |
|---|---|---|
| D1 | position/avg-price alerts: operator's manual shares → INFO (positive VP per strategy required); reconciler cycle now logs `duration_ms` | no CRITICAL for manual shares; reconciler duration ≈ 40–60 ms |
| D2 (resting + fresh ATR resting BUY) | no false "stuck accepted/pending" alerts when a live working proof exists (one off-loop coalesced write per sync pass, ≤ 64 orders) | **compare stuck-alert counts vs last week** — stricter proof raised alerts on historical replays (92→119 orders) because history lacks live receipts; if noisier live, tune next week |
| D3 | prior-session confirmation entries retired only when proven closed + flat (off-loop) | no 07:01 ERROR for previous-session fills; held positions keep their confirmation exit |
| D4 | momentum transient identity mismatch → INFO once; `cancel_entry_broker_identity_UNKNOWN` stays ERROR | fewer ERROR lines on momentum exits |
| D5 | legacy cancelled-partial child: durable fence + ONE incident (no exit-path change) | no duplicate incidents |
| Guard next-batch | `ExecCondition` closed-session skip; failure unit exit 0 when only the page fails | Mon 03:40 start runs (condition passes in session); 18:10 clean close |
| SYNC2X hygiene | ORB reconcile query rewritten (plan cost 1.08M → 6.4k) + guard + logical-account log line; WEEKMAINT reports `jit` setting | sync p50 ≈ 0.53 s |

Deferred to next week (board): **D2-momentum** (stripped — 52 failing controls), **O-1016-14 D2-ATR protective pairs** (no persisted complete-child evidence), O-1016-11 D5 recovery read.
Rejected tonight (52249c24, do not resurrect): Schwab flip exit via strict close receipts, Webull un-fence into release, FOR UPDATE on the OMS loop, per-order snapshot rewrite in the sync pass, D3 dropping held entries, new D1 CRITICAL.

## OPEN BOARD (not tonight) — owner / next step

| ID | Item | Next |
|---|---|---|
| O-1016-9 | MOMEXEC1 study: momentum execution price/speed/misses (13 signals/8 fills so far; misses, not slippage, carry the cost) | collect 100+ live trades, then conclude; broker switch (Lightspeed/DAS) PARKED |
| O-1015-10 | MOMCUTOFF1 (live momentum entries after 10:00) | watch & analyse until next week (operator) |
| O-1016-5 / O-1016-7 | XLAB late fill (root cause = MOMCANCEL1, installed) · FUFU 300 s live vs 600 s paper time exit | watch / board |
| O-1016-11 | D5 closed-episode recovery read | PARKED next week (class count from codex) |
| O-1016-8 | WB/ORB native-guard paths hold a DB session across broker awaits | next week |
| O-1016-6 | SEC → WEEK automatic chain (receipt ownership) | Sat supervised manual start |
| O-1016-1 | WEEKMAINT1 weekly maintenance | **Sat 10-17 supervised test with the operator** (fill `/etc/project-mai-tai/weekmaint.json` first; dry-run then real) |
| GC follow-up | gen2 GC pauses in pytest heap (90–130 ms) on new rate tests; prod impact unmeasured | lane L assessment card (gc.freeze after boot?) |
| O-1015-9 | VALIDATE-GREEN (CI test debt) | board |

## RULES ADDED TODAY (operator 10-16) — apply without asking

- **Bug fixes need no approval:** found bug → build, review, full test, VPS speed test, install after close, switch ON; tell him after. Ask him only when a fix would break trading with no fix available, or for a new rule.
- **Validation scan job** (every 15 min, both tools): its purpose is to catch every trading issue before he does — cross-broker price gaps, unwatched holdings, refused sells, plus (new today) **momentum price + speed gap per buy, momentum exit gap vs target/stop, momentum buy vs tape (+5/+30/+60 s), ATR exit price gap vs bracket/meant price**. Tools: `~/.claude/tools/mai-tai-fullscan-watch/run.sh` and `~/.claude/tools/mai-tai-fullscan/run.sh` (+ `extra.sql`, `exgap.py`, `mombuy.py`).
- **One speed test per bundle**, on the final composed head (VPS interleaved base vs candidate; GC-attributed or base-equal stalls are not regressions).
- **Every install reads loop numbers within 30 min** (sync-pass buckets) — SYNC2X was caught this way 30 min after the 18:47 install.
- **A large "alert fix" diff gets a hunk-to-item map before tests**; anything touching exits, cancels or sizing is out of scope unless named.
- **Broker:** stay with Schwab/Webull; professional broker parked as an option; keep measuring.

## MONDAY 10-19 FIRST READS (claude-1, unprompted)

1. 03:40 guard/paper start (MOMGUARDEND1) · 04:00 roll · loop numbers (sync p50, reconciler).
2. 07:0x scanner + watchlist adds + squeeze alerts (rule 9b) after the weekend maintenance restarts.
3. First Schwab momentum buy → MOMCANCEL1; first ORB entry → country route; first Webull bare fill → attach.
4. 15-min scans with the new price/speed gap sections; 18:10 guard close.
