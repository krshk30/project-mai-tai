# PMPRINT1 + PMFLIP1 - two pre-market cards, one review PR

## Assessment and scope

AGREE with both causes from Codex's own bounded reads and code inspection.
This is disclosed independent replication, not blind discovery: the operator
accepted that disclosure before the build. Prior assessment branch
`codex/pmprint1-independent-assessment` at `82d3f2e` contains the causal census,
alternative explanations and the three-item historical verdict. Raw evidence:
`/Users/velkris/.codex/pmprint1-evidence-20261005/` with SHA256SUMS.
Operator 2026-10-05 09:44 ET: "yes to both cards, go ahead."

PMPRINT1: the old stream path threw away the confirming ask, allowing SAIQ's
6.83 one-share print above a 6.7928 trigger while ask was 6.24. Dollar sizing
then dropped Webull on ask=0. PMFLIP1: VEEA's BUY flip was treated as a fill,
blocking its first qualifying 5.27 print 1.680 s later. The flip mechanism is
old; the Webull dollar-size failure became live with #1082.

Both new flags default FALSE, expected TRUE at the jointly reviewed install:
`strategy_schwab_1m_v2_pm_print_ask_confirm_enabled` and
`strategy_schwab_1m_v2_pm_flip_wait_enabled`. No install authorization here.

Only software rests before 09:30 are changed. RTH uses broker resting orders;
the broker hold-through-flip/fill path is unchanged. No exit, OMS, NFQ1, RPG1,
upper-cap, offset, timer, size amount or entry-window change. Stream retains its
existing ask_past_band skip; REST has no added upper cap. OMS still owns final
wire repricing, cap and QUOTE_DRIFT_CANCEL. A missing/below/stale ask consumes
no entry state. Successful drafts record ask/source/age/decision time; both
legs route and size from that same ask. Unsizable legs log ERROR plus a counter.

Flip-seen is separate from cross-taken. The existing bar-driven flip_no_fill
take-down still cancels an unfilled rest; there is no additional 30-second tick
cutoff. A cross still claims the existing once-only latch. BOOT/GAP holds,
position, watchlist removal and configured window checks remain conservative.

## Recorded replay

**Price-proxy replay; decision-cache timing UNMEASURED.** These are eligibility
tests with controlled fresh/flat/bar guards, not counterfactual fills or profits.
Original last/ask values come from Codex's retained raw pulls. Time is UTC in
the test fixture. Current dollars apply (600/300); historical shares are not
asserted as today's shares.

| Name | Trigger | Last | Confirming ask | Price-proxy result |
|---|---:|---:|---:|---|
| YMAT | 1.9975 | 2.00 | 2.01 | both legs |
| TNON | 3.5454 | 3.56 | 3.56 | both legs |
| SCNI | 2.3302 | 2.34 | 2.34 | both legs |
| MYSZ | 2.4399 | 2.44 | 2.44 | both legs |
| DAIC | 3.9849 | 3.989 | 3.99 | both legs |
| IMCC | 3.1195 | 3.13 | 3.15 | both legs |
| GLND | 3.0417 | 3.05 | 3.05 | both legs |
| TOPS 08:05 ET | 1.3453 | 1.3498 | 1.35 | both legs |
| DCOY | 4.3148 | 4.3169 | 4.32 | both legs |
| WHLR | 4.9325 | 4.94 | 4.94 | both legs |
| LGHL | 7.0149 | 7.02 | 7.04 | both legs |
| MEDS | 4.6438 | 4.6489 | 4.66 | both legs |
| NXL | 7.1412 | 7.16 | 7.16 | both legs |
| AMOD | 2.5693 | 2.57 | 2.58 | both legs |

**14/14 first-leg price proxies eligible; 5/5 trigger-reaching stray prints
blocked** (SAIQ, QNME, WHLR and two TOPS prints). The separate TOPS REST
incident also blocks. CLRO 5.5689 / recorded ask 5.55 / trigger 5.559 blocks:
this is the operator-approved correction to #1054's old 5.57 fixture, not a
claim to preserve its erroneous positive assertion.

PMFLIP replay population since the offset: 1/3 recoverable before take-down
(VEEA). WETO crossed later; CLRO's relevant print preceded the flip. VEEA's
08:33:04.184 print produces both legs after the 08:33:02.504 flip, once.
Own additional read at 10:37:52 ET is committed as `remaining-cases.json`:
VEEA tick 6794048 at 08:33:33.821, last 5.2691, size 1, quote 3243244 at
08:33:32.691, bid 5.26 / ask 5.27, verifies watching beyond settle-grace but
before bar take-down. WETO tick 6233046 is 645.330 s after take-down. MI's
09:15:20 order cdca0853-d931-40cb-a91a-36a90af03a20 records
QUOTE_DRIFT_CANCEL; 2.75 above-cap and later 2.79 drift regressions are tested.
All additional queries are READ ONLY, LIMIT 1/4, timeout 5 s, no Redis reads.

## Named tests

All names below are in `tests/unit/test_pmprint1_pmflip1.py`, unless noted.
Recorded prices are real; callback interleavings and freshness controls are
explicit test preconditions, not reconstructed historical cache timing.

| Card test | Test name |
|---|---|
| T1/T2/T3/T9 | test_t1_t2_t3_t9_recorded_stray_cannot_take_state |
| T4 | test_t4_fourteen_first_leg_price_proxies_emit_both_sized_legs |
| T5 | test_t5_clro_recorded_555_ask_blocks_second_print |
| T6 | test_t6_rest_stale_or_future_ask_blocks_then_fresh_quote_fires; test_t6_no_ask_never_uses_a_midpoint_or_burns_latch |
| T7 | test_t7_routing_uses_same_confirming_ask_not_rest_cache; test_t7_schwab_policy_reject_still_dispatches_webull; test_t7_unsizable_webull_leg_is_error_and_counted |
| T8 | test_t8_lghl_stream_cap_skip_then_recorded_rest_cross |
| T10 | test_t10_flag_off_reproduces_saiq_false_buy_and_zero_ask_webull |
| T11/F-T9 | test_t11_ft9_three_flags_move_then_flip_then_recorded_print |
| F-T1 | test_ft1_veea_flip_then_1680ms_late_print_emits_both_legs; test_ft1_recorded_veea_print_after_30s_still_watches_until_bar_takedown |
| F-T2/F-T7 | test_ft2_ft7_cross_then_flip_or_other_path_cannot_double_emit |
| F-T3 | test_ft3_flip_without_cross_keeps_existing_grace_and_next_bar_takedown |
| F-T4 | test_ft4_weto_real_tick_645330ms_after_takedown_never_reenters |
| F-T5 | test_ft5_sell_flip_ends_wait_before_a_later_print |
| F-T6 | test_ft6_existing_guards_still_block_during_wait |
| F-T8 | test_ft8_flag_off_reproduces_veea_flip_latch_miss |

Existing CLRO stream test is renamed to
`test_clro_second_print_with_recorded_ask_below_trigger_does_not_cross`.
Other explicit flag-OFF 5.57 probes remain legacy compatibility tests, not
representations of the actual CLRO decision.

## Verification

Final focused run: **972 passed** (20.00 s), including all v2 suites, PMREST1,
flip-owner probes, entry routing, catalog coverage, RPG1/NFQ1 composition and
OMS mirror/EH regressions. New combined module: **56 passed**.

Full `tests/unit`, same macOS venv and PYTHONPATH=src:

| Tree | Failed | Passed | Exact failed-name comparison |
|---|---:|---:|---|
| main a80b51816abf0aefc269f0fdfc473468fd3fe62c | 56 | 5,412 | baseline |
| combined build | 56 | 5,468 | identical, sorted diff empty |

Raw outputs: /tmp/pmprint-main-unit.txt, /tmp/pmprint-head-final.txt,
/tmp/pmprint-focused-final.txt. Full local suite is NOT green. Baseline shell,
sync-checkout, log-retention, throughput and unattended-upgrade failures are
unchanged. Ruff and literal log-marker isolation PASS.

**10/10 substantive mutations RED**: lower check, existing REST age bound,
stream ask preservation, flip blinds the rest again, take-down removed,
cross latch removed, Webull uses Schwab dollars, routing loses confirming ask,
emit accepts expired ask, unsized Webull ERROR becomes INFO. Each was isolated;
pytest rc=1 required, runtime/import errors do not count as kills.

Main comparison is the exact PMREST1 merge a80b51816abf0aefc269f0fdfc473468fd3fe62c.
The macOS baseline differs from the reviewer Linux baseline; compare exact failed
names locally, never present a green full suite while baseline failures remain.

The freshness reason-only mutation survives equivalently: the existing
_resting_ask_evidence returns ask=None for stale/future evidence, so the separate
no-ask check still refuses. The substantive age-bound mutation targets that
existing helper instead. Mutation harness is in-memory, isolated per process;
neither source nor production is mutated.

## Remaining limits

Decision-cache historical timing, actual fill/profit and installed behavior are
UNMEASURED/UNEXERCISED. Owner-UNKNOWN, unchanged-rest 09:30 boundary, F2 second
entry question, low-print exits, shared health cursor and 07:00 late start are
explicitly excluded. Evening deployment requires independent exact-head pin,
merge and operator exact-SHA GO for the folded PMREST1 plan. No restart here.
