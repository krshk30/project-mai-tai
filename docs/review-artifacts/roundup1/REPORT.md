# ROUNDUP1 Build Evidence

October 5, 2026. Frozen base: `7e10baf0319da796b84934fe38994f6db4fcfc0b`.

**Assessment AGREE: the old nearest-tick entry rounding can buy below the
four-decimal rule price. The default-OFF implementation is built on an isolated
branch, not merged or installed. This is a DRAFT, not a pin request: #1093 has
not merged, so its required rebase and combined ON/OFF proof remain PENDING.
Generic pre-switch resting-order restart coverage outside an RPG ticket is
also not proven end-to-end. Nothing in this report authorizes an install.**

## Early Generic Restart Blocker

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
These results do not replace the pending merged-main full-suite pair,
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
The catalog has 128 boolean entries and the combined check denominator is 144.

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

## OFF Identity And Tests

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

## Mutation Controls

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

- #1093 must merge first; rebase this branch and prove that combined code with
  tonight's exact flag set, ROUNDUP OFF and ON, before asking for a pin.
- A broker rest that predates switch-on but has no RPG ticket is not yet
  covered by an end-to-end bot restart test. The controlled state-preservation
  and accepted-ticket restore tests are narrower; they do not waive this edge.
- No live acceptance, partial-fill outcome, counterfactual fill delay or P&L
  is measured. Partial-fill tests are controlled interleavings around SCKT,
  not claims of a recorded SCKT partial fill. Sub-dollar/dollar-boundary tests
  are adversarial cases because the census contains no sub-dollar order.
- The six PM checks are price proxies; full decision-cache timing is UNMEASURED.
- No deployment plan, setting activation, service restart or production write.
  No unrelated branch, agent task or shared handoff is changed by this lane.
