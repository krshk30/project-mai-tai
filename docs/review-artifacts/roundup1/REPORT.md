# ROUNDUP1 Build Evidence

October 6, 2026. Current merged/rebased base:
`7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`, tree
`ee6f058c248eeebf475fd392845eadfef7af59eb`.
Historical pre-rebase evidence below used base
`7e10baf0319da796b84934fe38994f6db4fcfc0b`.

**Assessment AGREE on rounding. This remains a default-OFF DRAFT, not a pin
request or install candidate. The eight restart diagnostics and five integration
assertions now pass in a 543-case local focus. Generic recovery is a tested
checkpoint, NOT safe to enable yet: bounded archival discovery/clearance and
exact-target cancel dispatch remain unresolved. Nothing here authorizes an
install, pin, merge or flag activation.**

## October 6 Checkpoint - Not Ready

Own explicit diagnostic + new discovery tests + ROUNDUP focus: **496 passed in
6.62 s**, raw `/tmp/roundup1-legacy-working-20261006.log`. This includes eight
previously failing startup cases, 85 new recovery cases and 403 ROUNDUP cases.
Five obsolete assertions now follow installed RPGSTUCK1 account-local ownership;
three mirror controls preserve primary generation/quantity when only Schwab is
blocked. This does not weaken the blocked leg or permit a second same-slot buy.

The new recovery book runs before service entry callbacks and refreshes during
position polling, off the quote callback. It joins exact strategy/account/order/
intent identities; Schwab request prices and explicit broker-origin Webull
accepted reports prove wire pairs. Missing/conflicting evidence remains owned;
partial/fill consumption does not become reusable after a terminal refresh.
Flag OFF performs zero recovery reads. Cached admission performs no DB queries.

Fixture correction is disclosed: the retained SCKT/PFSA prices are permuted into
accepted/unfilled, no-ticket scenarios. Segment comes from the persisted record,
not the replay clock; TradeIntent payload has the production metadata wrapper.
The retained Webull event has unknown origin; the controlled test explicitly
supplies broker origin and exact matching IDs/slot metadata. Those controls are
NOT proof of a historical accepted-order restart or historical duplicate.

**Readiness blockers found by source review:**

- ON adds a startup refresh and one per position poll: seven SELECTs with matching
  orders, five without. All historical STOP_LIMITs/events/open intents are scanned;
  row volume and cache growth are unbounded, latency is unmeasured. A terminal row
  missing exact broker zero-fill proof becomes unknown and may block a future
  re-add indefinitely. No status-only, latest-buy, or age-based clearance is allowed.
  A bounded archival/clearance design is still owed, not hidden by controlled tests.
- `_process_cancel_intent` calls `OmsStore.find_open_order_for_cancel`. If the exact
  client/broker target is no longer open, that method falls through to the most
  recently updated open order for the strategy/account/symbol. Hydrated cancels
  carry exact IDs, but those IDs alone do not prevent this fallback. A race with a
  newer order can therefore target the wrong entry. OMS code is unchanged here;
  fixing that dispatch safely requires explicit scope/review, not an implicit
  rounding-only claim. No production cancel or ledger write was performed.

Existing nine in-memory mutations revalidated **9/9 RED** against the clean
403-case control; `/tmp/roundup1-20261006-mutation-*.log`. They cover existing
rounding/wire-sizing/RPG-restore behavior only, NOT new generic recovery.
New recovery mutation controls are **7/7 RED**; raw
`/tmp/roundup1-20261006-legacy-mutation-*.log`. The terminal source-site probe
initially missed the guard's indentation and returned UNMEASURED; the corrected
in-memory terminal-status mutation is RED (8 failed / 79 passed).
No unexplained non-test failure or on-disk source mutation counts as RED.
Initial full-main run **56 failed / 5885 passed**, 246.06 s, included system-Python
subprocess mismatches; correcting PATH makes all 16 sync-checkout tests pass.
A complete corrected-PATH pair is recorded below, not conflated with that run.

Own low-priority, read-only aggregate on the box at **06:40:43 ET 10-06** used
`LIMIT 20`, READ ONLY transaction and a five-second statement timeout:

| Historical STOP_LIMIT status | Rows | Missing linked intent | Missing segment / attempt / slot identity |
| --- | ---: | ---: | ---: |
| cancelled | 3864 | 0 | 3846 |
| filled | 622 | 0 | 270 |
| rejected | 1492 | 0 | 1041 |

Range starts July 23 and ends October 5. Total **5978 rows**, 5157 missing at
least one newer identity field. This is a bounded aggregate, not a new read of
all payloads or an assertion that historical orders are live. It proves the
draft discovery query would encounter a large legacy archive, not merely the
two controlled restart rows. The initial trader query was refused on the
root-only env file before SQL; the root read performed no writes.

First complete same-PATH head check was **50 failed / 6379 passed**, 259.05 s,
versus main **47 failed / 5894 passed**. Three added names: two ALL_ON catalog
checks still expected147 instead of148, and the default-OFF exit-coverage
harness lacked the new refresh method. Catalog counts are corrected without
changing the live ALL_ON values; default-OFF position polling now omits the
recovery call altogether. The head run also preceded two additional cold-start
unreadable tests. A fresh frozen-head run is recorded below, not this provisional pair.

### Final October 6 Local Checkpoint

Focused controls: **543 passed / 8.75 s** in
`/tmp/roundup1-final-focus-20261006.log`, including 87 new recovery tests,
403 ROUNDUP cases, eight explicit startup diagnostics and ALL_ON/default-OFF
exit-coverage regression controls. Scoped Ruff and `git diff --check` PASS.

Same interpreter, virtualenv first on PATH, each checkout's PYTHONPATH=src:

| Checkout | Failed | Passed | Duration | Raw log SHA256 |
| --- | ---: | ---: | ---: | --- |
| Main application base 7823a6fa | 47 | 5894 | 238.49 s | e2ec7223de97e8047441b551017c401850ce364d17f787ae77d970067c2c22d8 |
| Final default-OFF working checkpoint | 47 | 6384 | 258.23 s | bea9a56429580aafeaa4535fd4ea12fb7e8b75b4966d8e985aba4cd19bdd5d2a |

Raw `/tmp/roundup1-main-7823-unit-20261006-path.log` and
`/tmp/roundup1-head-final-unit-20261006-path.log`. Failed node sets are identical,
zero additions/removals. One head summary line has interleaved stderr appended
after its parameter closing bracket; comparison strips that absolute-path
warning suffix, not the test identity. The 47 are 44 fanout-installer tests,
one log-retention installer, one unattended-upgrade installer and one real
dead-consumer throughput test. This is the actual local baseline, not a claim
that Claude's earlier 48 or pre-PATH 56 failed-name sets were reproduced.

No CI-green, historical accepted-wire restart, complete simultaneous ROUNDUP-ON
composition, activation safety or review-readiness claim follows. #1095 stays
DRAFT. Parent requested a scope ruling: bounded archival/clearance plus exact
target dispatch in a separate prerequisite PR versus an explicitly widened
#1095. No OMS change, merge, flag edit, service action or ledger write.

## October 5 Rebase And Blockers - Historical Evidence

Tested rebased source/test head: `c29db1165c24796adf0e7fad5a320edcbc56e383`.
Rebase is **not patch-identical**. All six conflict hunks in two files were
resolved explicitly; no other file had a content conflict. See retained
[range-diff](REBASE_7823_RANGE_DIFF.txt).

- Strategy import hunk: retained main's `old_buy_proven_clear` and added
  ROUNDUP's `proven_resting_pair` and canonical price helpers.
- Strategy placement-state hunk: ROUNDUP per-leg wire stop/limit assignments
  now live under main's `not primary_blocked` / `not webull_blocked` guards,
  alongside quantities; an owned leg's stored pair is not overwritten when
  placing a different, unblocked leg. Main's generation guards remain.
- Strategy mirror hunk: retained `not webull_blocked`; applied ROUNDUP's final
  per-leg wire limit to sizing inside that guard.
- Strategy `_rpg_leg_owned` hunk: retained main's delegation to
  `_rpg_entry_owned(..., account=account)`, not the older ROUNDUP override.
  Main's per-account clearance, requested-leg acknowledgement and
  `old_buy_proven_clear` handling stay intact. The auto-merged ROUNDUP
  placed-wire proof checks remain; no generic no-ticket protocol is added.
- Catalog test name hunk: retained main's all-ON live-set assertion, renamed
  its denominator to 148 for the new default-OFF ROUNDUP check.
- Catalog count hunk: retained the merged main's 147-check basis, plus one
  ROUNDUP owner check = 148. The old branch's 143 -> 144 basis is obsolete.

Catalog/expected-settings tests, OMS entry-cap changes, handoff wire feedback
and service feedback changes auto-merged without conflicts. Existing main
NFQ/RPG metadata and callback ownership changes were not replaced wholesale.
Catalog has 128 boolean entries / 148 total consumer checks; the four PM/RPG
keys remain expected TRUE, NFQ/GAPHOLD remain TRUE, and ROUNDUP is expected FALSE.

Fresh current-tree results:

- Explicit outside-discovery diagnostic: **8 FAIL, 0 errors**, 1.12s;
  [raw assertions](LEGACY_RESTART_GAP_7823_REBASE.xml). Both accepted old-wire
  pairs remain `(0, 0)` after real startup/poll, and unproven cases permit
  controlled strategy drafts, not proven venue duplicates.
- ROUNDUP unit focus: **395 PASS / 5 FAIL, 0 errors**, 2.94s;
  [all 400 exact cases](ROUNDUP_FOCUS_7823_REBASE.xml).
- Relevant composition/regression focus: **663 PASS**, 58.44s;
  [all selected files/cases](COMPOSITION_FOCUS_7823_REBASE.xml). This includes
  PMPRINT/PMFLIP/PMREST, the merged RPG restart/all-ON/clock/loop suites,
  NFQ, catalog, sizing and resting regressions. ROUNDUP is default OFF in
  the merged RPG ALL_ON fixtures: this result is not proof of simultaneous
  ROUNDUP ON + all four PM/RPG flags + NFQ/GAPHOLD ON across every required edge.

Exact five focus failures (all under `tests/unit/test_roundup1.py`):

- `test_restart_restores_recorded_accepted_wire_not_new_calculation_and_never_duplicates[webull-False]`
- `test_unproven_or_malformed_placed_price_keeps_both_admission_guards_closed[None]`
- `test_unproven_or_malformed_placed_price_keeps_both_admission_guards_closed[wire1]`
- `test_unproven_or_malformed_placed_price_keeps_both_admission_guards_closed[wire2]`
- `test_unproven_or_malformed_placed_price_keeps_both_admission_guards_closed[wire3]`

Disposition: these assertions expect a single unproven Webull RPG ticket to
block both primary and mirror drafts. Main's retained per-account semantics
allow a new primary draft when only Webull owns that ticket. They remain
visible **integration blockers requiring fresh review**; neither the tests
nor the admission guards were weakened to manufacture green. This does not
resolve the separate generic no-ticket restoration gap.

Fresh full-unit pair: **INCOMPLETE / NOT RE-RUN TO COMPLETION on 7823**.
At the parent's explicit publication-priority request, both owned test sessions
were stopped through their session APIs (both exited 2). No other test/session
was stopped. These partial outputs are not completed suite evidence; no current
pass/fail totals or complete failed-name diff are claimed:

- [main interrupted text](MAIN_7823_UNIT_INCOMPLETE.txt) and
  [main interrupted XML](MAIN_7823_UNIT_INCOMPLETE.xml)
- [head interrupted text](HEAD_7823_REBASE_UNIT_INCOMPLETE.txt) and
  [head interrupted XML](HEAD_7823_REBASE_UNIT_INCOMPLETE.xml)

Prior 5,500/5,900 counts and 9/9 mutation results below belong only to the old base. Current
mutations are **NOT REVALIDATED**: the existing mutation runner's blanket
exit-code check would be invalid with five control failures. Combined ON/OFF
proof, current clean controls/mutations, and generic restart safety remain
blocked; no ready-for-review or install claim follows from the passing 663.

## Pre-Rebase Generic Restart Diagnosis

At unchanged head `38d21fa3b122cbbc7c02b163c1fc84d6b5872a59`, this is a
production restoration/admission gap, not a test-only coverage omission.
No source change, rebase, push, deployment, or production write accompanies
this diagnostic. The mandatory #1093 merge-SHA instruction is still pending.

`SchwabV2BotService.run` (services/schwab_1m_v2_bot.py:761) runs startup
exit coverage, then `_rpg_handoff_pass` (:1378). That pass restores wire proof
only from handoff jobs. `_position_poll_pass` (:1873) and
`_fetch_position_maps` (:2343) hydrate held/in-flight quantities, not the
legacy accepted order's exact wire pair, broker identity, generation, and
resting latch; the latter reads only the configured primary account.
No-ticket accepted STOP_LIMITs do not receive the RPG restore behavior.

The explicit diagnostic `test_legacy_restart_gap.py` lives outside normal
discovery (`pyproject.toml` sets `testpaths = ["tests"]`). It is an intentionally
failing, explicitly invoked blocker characterization, not a claimed passing
unit gate. Keeping it in the review artifacts separates unresolved required
invariants from CI gates for the existing implementation; its failures remain
published here and readiness stays blocked. It drives the real service `run`, startup exit coverage, RPG pass,
and first position poll with an independent-connection local in-memory book.
Only network/heartbeat publication and unrelated background loops are stubbed.
There is no handoff snapshot/ticket; RPG request metadata is removed for this
controlled permutation. Retained SCKT/PFSA prices are replayed as accepted,
unfilled orders, **not** claimed to have had that state at a historical restart.

Fresh diagnostic: **8 assertion failures / 0 passes**, in 0.89s:

- `test_no_ticket_startup_preserves_accepted_old_wire`: first/reclaim x
  Schwab/Webull, four failures. Observed restored pair `(0.0, 0.0)` instead
  of PFSA `3.77/3.79` or SCKT `1.06/1.07`.
- `test_no_ticket_unproven_wire_blocks_duplicate_rest`: first/reclaim x
  Schwab/Webull, four failures. After real startup/poll, controlled eligible
  strategy trackers allow a new primary draft without accepted-wire proof.
  This tests draft-level admission, not the entire outbound service route;
  it is **not** proof a duplicate reaches a venue, nor of a historical duplicate.

Raw XML: [eight assertion failures, zero errors](LEGACY_RESTART_GAP_38d21fa3.xml).
Existing unit focus remains **400 passed** in 3.28s;
raw XML [400 passing unit cases](LEGACY_GAP_FOCUS_38d21fa3.xml).
These historical results do not replace the pending merged-main full-suite pair,
composition proof, or safety mutations for an eventual production fix.

Reproduce explicitly from the ROUNDUP worktree (expected exit code **1**):

```sh
env PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python \
  -m pytest -p no:cacheprovider \
  docs/review-artifacts/roundup1/test_legacy_restart_gap.py -q --tb=short
```

Both test families parameterize `leg = schwab, webull` and
`slot = first, reclaim`; all eight exact case names and assertions are retained
in the diagnostic XML. Evidence is from the pre-rebase tree `38d21fa3`, not
the yet-to-be-merged #1093 composition. A fresh post-rebase diagnostic is
required before publishing the DRAFT blocker head; no fresh full-suite pair
has been run or claimed passing for that future tree.

A safe correction needs generic evidence hydration plus fail-closed admission,
including exact account/strategy/order/episode binding, both legs, persisted
first/reclaim consumption, partial/filled/terminal states, conflicting or
unreadable evidence, and startup ordering before entry callbacks. This is a
broader ownership protocol than tick rounding alone. Scope alignment is
required before expanding production code; #1095 is **not ready** tonight on
the current proof. ROUNDUP remains default OFF, not activated.

## Price Contract

The accepted 13:58 ET correction is implemented: format the existing float
line-times-offset product to four decimals exactly as today, then use Decimal
ROUND_CEILING to the broker tick. No epsilon, float ceil, or alternative exit
reference is introduced. The initial four-decimal formatting can be at most
0.00005 below the exact product; the ceiling guarantee is relative to that
canonical four-decimal input, not an unretained infinite-precision line.

`4.510000001 -> 4.5100 -> 4.51`; `1.06359 -> 1.0636 -> 1.07`.
Below $1 the tick remains 0.0001; at/above $1 it is 0.01. A sub-dollar ceiling
landing at $1 is re-evaluated. Each unchanged adapter retains its own exact-$1
representation. The limit is computed from the sent trigger using today's
four-decimal band formula and each adapter's formatter/lift, not a new ceiling
rule for all prices. Both one-tick lifts remain.

There is ONE canonical trigger reference in entry_price/reference_price, not a
legacy bracket reference. Existing bracket percentages and `_schwab_round`
produce PFSA stop/limit **3.78/3.80**, target/protection **3.97/3.48**, as ruled.
PM software crosses still submit ask-priced LIMITs: the rounded trigger is
their decision reference, not a claim their execution price equals the trigger.

Default-OFF setting: `strategy_schwab_1m_v2_resting_buy_round_up_enabled`.
The catalog expects FALSE; activation needs a later exact-SHA GO, not tonight.
The old-base catalog had 128 boolean entries and 144 checks. The current
merged/rebased catalog still has 128 boolean entries, now 148 consumer checks.

## Recorded Orders And Six PM Crosses

Own raw pull: `/tmp/roundup1-independent-capture-20261005.json`, read
2026-10-05T17:48:56.406944Z, SHA256
`76e88964e0a381e0f958eb07657a045a45ec3331cf57a83fca5b6832711b7fba`.
The bounded, read-only queries are retained in `scripts/roundup1_read_only_capture.py`;
there is no Redis snapshot read or production write. The full captured fixture
is `tests/fixtures/roundup1/orders_179.json`.

See [all 179 orders](ORDER_TABLE_179.md) and [machine-readable replay](WIRE_REPLAY_179.json).
Each order has old/new stop, limit, shares, bracket target and protection.
Population: **179 / 75 filled** = STOP_LIMIT **173 / 72** plus PM LIMIT **6 / 3**;
112 Schwab and 67 Webull rows. OFF/ON replay uses actual OMS decoration and
adapter formatters, not a standalone duplicate of their rounding formulas.

| Numeric change | Count / Denominator |
|---|---:|
| Wire stop moves upward | 84 / 173 STOP_LIMIT |
| Wire limit changes | 98 / 179 |
| Quantity changes at recorded notional | 5 / 179 |
| Bracket target changes at unchanged +5% rule | 53 / 179 |
| Protection changes at unchanged -8% rule | 47 / 179 |

These replace the historical Step 0 naive 88/104/6 line reconstruction: the
implementation replay uses the already-recorded four-decimal entry input.
No new counterfactual fill-time, P&L, or lost-winner claim is made. Missing/zero
recorded notionals retain historical fixed quantities, not retroactive 600/300.

The five quantity changes are all SCKT:

| Account / Client Suffix | Old Stop / Limit / Shares | New Stop / Limit / Shares |
|---|---|---|
| Schwab / 1f63cbaaf087 | 1.09 / 1.10 / 545 | 1.10 / 1.11 / 541 |
| Webull / 074e9d445a42 | 1.09 / 1.10 / 273 | 1.10 / 1.11 / 270 |
| Webull / 494aac58d0c2 | 1.08 / 1.09 / 275 | 1.09 / 1.10 / 273 |
| Webull / 69a4f7276ad3 | 1.07 / 1.08 / 278 | 1.08 / 1.09 / 275 |
| Webull / 9a10bc5b0102 | 1.06 / 1.07 / 280 | 1.07 / 1.08 / 278 |

The six software LIMIT rows are replayed below through both stream and REST
cross checks using recorded print/ask proxies from the existing real-case
fixtures. Original decision-cache timing, slot/admission timing and later
cross/fill outcomes are UNMEASURED, not inferred from these controlled clocks.
Their ask-priced wire projections in the 179-row table are conditional on an
eligible cross, not assertions all six would still submit.

| Symbol / Client Suffix | Print / Ask Proxy | Raw -> Rounded Trigger | Cross Result |
|---|---|---|---|
| LGHL / 4b8234dd9ee4 | 7.02 / 7.04 | 7.0149 -> 7.02 | eligible on both paths |
| MEDS / 2ede8d74c86c | 4.6489 / 4.66 | 4.6438 -> 4.65 | no cross on this print; rest waits |
| NXL / 2a3d8472d70c | 7.16 / 7.16 | 7.1412 -> 7.15 | eligible on both paths |
| AMOD / 4355c5cbba77 | 2.57 / 2.58 | 2.5693 -> 2.57 | eligible on both paths |
| SAIQ / bda3ad57da92 | 6.83 / 6.24 | 6.7928 -> 6.80 | ask-confirm still blocks |
| MI / 5f686b216fb4 | 2.749 / 2.75 | 2.7288 -> 2.73 | stream still skips ask above final 2.74 cap; REST may draft, OMS still prices/refuses |

MI's retained OMS ask/wire was 2.74. That is distinct from the 2.75 strategy
proxy; no claim is made that both caches contained the same ask at one instant.
No upper cap is added to the REST cross gate.

## Readers And Preservation

Changed readers/seams are explicitly limited to resting-entry data:

| Reader / Writer | Final-Pair Handling |
|---|---|
| `_resting_trigger_for_line`, `_queue_resting_place` | canonical stop; per-leg final limit; shares sized at own limit |
| `_active_resting_trigger`, `_active_resting_cap` | prefer stored/proven per-leg pair, not line recomputation |
| `on_stream_trade`, `_eh_resting_cross_check` | rounded trigger; existing stream cap uses final wire limit |
| `_build_webull_fanout_draft`, `_fanout_rth_resting_cross` | same final trigger; per-leg pair metadata |
| `_reprice_resting` | PM atomic update carries final pair; broker rest keeps quantity/generation if final pair is unchanged |
| `_queue_resting_cancel` | quantity actually placed, old per-leg proven trigger, never a new sizing calculation |
| `rpg_handoff_authorization`, admission guards | restore accepted recorded wire; missing/malformed proof blocks duplicate placement |
| `HandoffJournal.reconcile_feedback`, bot handoff poll | opt-in accepted wire capture; Webull uses broker-origin accepted event, not raw strategy price |
| OMS `_band_capped_marketable_limit` and two entry callers | opt-in final cap for EH rest / RTH resting fanout; OFF default unchanged |

NFQ stamping, deferred distance/no-chase, Webull wire-shape and OMS drift checks
are not rewritten: they receive the final stop/limit fields from these seams.
Freshness windows remain unchanged. No quote-hot-path DB query is added. With
the flag ON, RPG reconciliation adds at most one accepted-event lookup per
accepted Webull ticket per feedback pass; with it OFF that lookup is absent.

Switch-on alone does not emit a cancel, replace, resize or second buy in the
recorded-wire state test. Accepted RPG restore reads SCKT 1.06/1.07 and PFSA
3.77/3.79, not the new calculation. If the accepted wire is unproven it waits.
The unchanged-wire reprice boundary is a controlled 0.51% perturbation of
recorded GIPR 1.2166, explicitly not a recorded reprice event.

Shared `_schwab_round` and Webull `_round_to_tick`, all adapter source, Webull
fill-derived protection pairs, software/confirmation/ATR SELL methods, ORB and
Momentum source are unchanged. AST comparison of OMS methods finds changes
only in the three entry-cap methods listed above. Existing exit/ORB/pair
regressions: **179 passed**; raw `/tmp/roundup1-exit-regressions.txt`.

## Historical OFF Identity And Tests

Against a separate frozen-main checkout, deterministic strategy drafts for all
179 inputs are byte-identical OFF (including quantities and metadata):
`/tmp/roundup1-strategy-off-baseline.json` and
`/tmp/roundup1-strategy-off-head-final.json`, both SHA256
`0c1e89e60fb0356e2730f54b9e2c6f4ea63eca5c3e602b41c0ed8c9790a051e1`.
Full OFF wire/metadata/bracket replay is also byte-identical:
`/tmp/roundup1-off-baseline.json` and `/tmp/roundup1-off-head.json`, both SHA256
`7e241c33e16f6b2a63aafa6739d3305ef9921de722fa890bb864ae3efe5ec520`.
These compare identical inputs and fixed UUIDs; they are not live broker calls.

ROUNDUP1 tests: **400 passed**. Combined PMREST1 / PMPRINT1 / existing RPG1 /
NFQ1 / catalog focused set: **898 passed**. This existing-RPG result does NOT
stand in for #1093 composition. Scoped Ruff and `git diff --check` pass.

Key test names in `tests/unit/test_roundup1.py`:

- `test_recorded_orders_off_keeps_wire_quantity_and_bracket` (179 rows)
- `test_recorded_orders_on_never_rounds_trigger_down_and_sizes_wire_limit` (179 rows)
- `test_existing_four_decimal_format_precedes_ceiling`
- `test_sckt_strategy_to_both_wire_limits_sizes_from_108` (first/reclaim)
- `test_pfsa_bracket_uses_the_single_sent_trigger_not_legacy_exit_reference`
- `test_six_pm_print_proxies_compare_to_rounded_trigger` (both paths)
- `test_switch_on_alone_preserves_working_sckt_wire_quantity_and_no_buy`
- `test_small_band_existing_pair_lift_and_dollar_representation`
- `test_oms_no_chase_reads_final_wire_cap_not_raw_formula`
- `test_restart_restores_recorded_accepted_wire_not_new_calculation_and_never_duplicates`
- `test_unproven_or_malformed_placed_price_keeps_both_admission_guards_closed`
- `test_partial_fill_feedback_never_resizes_or_rebuys_remainder`
- `test_reprice_threshold_crossed_but_proven_wire_pair_unchanged_sends_nothing`
- `test_rounded_stop_at_or_below_current_ask_cannot_place`

Full unit-suite pair and failed-name diff are recorded in
`FULL_SUITE_FAILED_NAME_DIFF.json`: **main 5,500 passed / 56 failed; head
5,900 passed / 56 failed**, identical failed names (new=0, absent=0). Both use
macOS ARM64 / Python 3.12.13 / the same venv and frozen actual Git checkouts.
Raw main `/tmp/roundup1-main-unit-final.xml` and
`/tmp/roundup1-main-unit-final.txt`; head
`/tmp/roundup1-head-unit-final-400.xml` and
`/tmp/roundup1-head-unit-final-400.txt`. The real Git baseline supersedes the
earlier archive-only run, whose extra executable-mode failure was a missing
`.git` artifact, not a defect fixed by this PR. No archived-run improvement
is counted as a fix.

## Historical Mutation Controls

`scripts/roundup1_mutation_probe.py` changes one function in memory per fresh
process; it never edits production source. Each runs all 400 ROUNDUP tests.

| Mutation | Verdict / Failed Tests |
|---|---:|
| ceiling -> nearest | RED / 106 |
| ceiling -> floor | RED / 202 |
| wrong sub-dollar tick | RED / 1 |
| sizing at raw limit | RED / 2 |
| existing pair lift removed | RED / 2 |
| PM reader on raw trigger | RED / 8 |
| restore recalculates trigger | RED / 2 |
| separate legacy exit reference reintroduced | RED / 1 |
| unproven placed restore releases admission | RED / 5 |

Raw outputs: `/tmp/roundup1-mutation-<name>-final-400.txt`; JUnit:
`/tmp/roundup1-mutation-<name>.xml`.

## Not Covered / Next Gate

- #1093 merge and conflict-resolved rebase are complete; integration changes
  require fresh review. Prove combined code with the exact flag set, ROUNDUP
  OFF and ON, only after resolving the recorded blockers and before any pin.
- A broker rest that predates switch-on but has no RPG ticket now has an
  explicitly invoked real-startup diagnostic, with eight failing assertions.
  The controlled state-preservation and accepted-ticket restore tests are
  narrower; they do not waive or fix this edge. Five current ROUNDUP unit
  integration failures also remain unresolved.
- No live acceptance, partial-fill outcome, counterfactual fill delay or P&L
  is measured. Partial-fill tests are controlled interleavings around SCKT,
  not claims of a recorded SCKT partial fill. Sub-dollar/dollar-boundary tests
  are adversarial cases because the census contains no sub-dollar order.
- The six PM checks are price proxies; full decision-cache timing is UNMEASURED.
- No deployment plan, setting activation, service restart or production write.
  No unrelated branch, agent task or shared handoff is changed by this lane.
