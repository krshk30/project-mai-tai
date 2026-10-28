# C1 21:29 two-group correction / source-only handoff

New owned worktree /private/tmp/mai-tai-l14-firstbar-c1-two-group-20261023.
Base Main1c4f7e1049ea1305a79fead3097709cf4db20368. Older54e/af7/2c5 wide route
proposal is NOT in this branch; none is activated/cherry-picked. Main c903/04
receipt and 25c4 witness-hash ledger are immutable. No tests/imports/compile/
model/reference/PG/provider executed by author. Static diff inspection only.

## Exact behavior changes / whole call graph

1. FirstBarPosition -> standalone verified load_target_rule at construction ->
   the EXACT live target_price.py target_limit_price function. Position retains
   the sealed function, not disk IO on every print. Each eligible target check
   uses ceiling(current running per-account VWAP * Decimal1.05), with the live
   .01 grid for >=1, .0001 below1. Print must reach wire limit. Limit modeled
   execution is that target, not nominal threshold or symbol-specific expected.
2. Existing stop predicate current eligible print <= running VWAP*.92 remains
   unchanged. Its execution price is THAT first eligible round-lot print, which
   may trigger and execute at the same causal key (at/after trigger), not a forced
   later-key delay. No reference next-print confirmation/lookahead. BBCI3.87 only
   occurs if the actual qualifying print is3.87; no hardcoded case output.

No entry/allocation/planning/typed-bar/oracle/SELL/context changes. No native
route assumptions introduced. Shared physical print conservation, per-account
VWAP, partial remainder, first-fill/X3 clock, no-X4 policy, causal barclose
availability, excluded-LASTX treatment, UNKNOWN eligibility, LIVE fallback and
SELL/session precedence stay intact. Legacy VOLBODY Position/exits.py untouched.

The loader reads/hash-verifies ONLY the standalone stdlib leaf, then compiles
those sealed bytes under a private name; it never imports broker_adapters or
its eager alpaca/schwab/webull imports. This execution is for Main runtime, not
performed by the worker. Every construction verifies the actual file hash;
sealed-function cache is keyed by its exact bytes/path. Mutation refuses.

## Explicit versions / exact hashes

Result version: ATRSIM_FIRSTBAR_REPLAY_V2_A2_CEILING_CAUSAL_STOP_PRINT.
Policy label: C1_20261023_2129_A2_CEILING_FIRST_ELIGIBLE_STOP_PRINT.
Result assumption and target/stop price_basis distinguish new policy. Fill
policy/identity/schema remain unchanged; native route/queue remains UNKNOWN.

| File | SHA256 |
| --- | --- |
| firstbar.py (unchanged) | 29bf8d2282a0d3ee55d43a6676ddc1e817c51a57c41b6ea14fcb866484697302 |
| firstbar_exits.py | ee38e47e7188c2dec75cb5f58edad7b897841cdb745ed4be8facaedccffb11d2 |
| firstbar_replay.py | 4ef286903bebc2c87cf6d4ddc8c93b51311cfc16228d9d0840f4234b5befc2c9 |
| firstbar_target_rule.py (new) | a4ed9a1e387a3815f08a5c61e47bbed59c57e8b83bca39f749965bc1fe12d0c6 |
| broker_adapters/target_price.py (unchanged A2 leaf) | 591a4f6473db1aa200e0b96175a3276d0cd6879102acc5c4dcd6938ff9290e6f |
| tools/atr_firstbar_study_replay.py | 038319ff417695c136b1e98f486adfb3decbae26a9d1d337ed4ad648234f7af2 |

Runner has14 actual producer source pins: old12 + firstbar_target_rule.py +
the standalone broker_adapters/target_price.py. Each is hashed from actual
runtime files; the leaf must match the fixed live SHA above. It emits explicit
exit_policy_version. Runner refuses BEFORE model work if projection binding
still declares old V1. No source binding is guessed or skipped.

## Required Main-owned binding/bridge batch BEFORE one173

No Main/V2 binding, projection, store, publication or UI file edited here.
Main must set FIRSTBAR_ENGINE_HEAD to this actual committed author head (supplied
in handoff message, NOT embedded self-referentially), FIRSTBAR_RESULT_VERSION
to V2 above, update firstbar_exits/replay hashes and add the new loader's hash.

Verify standalone leaf separately at actual root.parent/broker_adapters/target_price.py
using the fixed SHA. Do NOT blindly put ../broker_adapters/target_price.py in the
old FIRSTBAR_PINS loop: load_firstbar_engine currently maps ANY ../ name to
momentum_paper.conditions and would then claim the wrong import root. Four
ordinary SIM module pins + a separate verified leaf check preserve strictness.

For the successor publication producer descriptor, extend the exact old12 with:
- src/project_mai_tai/atr_simulation/firstbar_target_rule.py
- src/project_mai_tai/broker_adapters/target_price.py
The bridge currently requires exact PRODUCER_FILES membership. Main/V2 must add
an explicit reviewed V2/14-member producer generation, not accept arbitrary
subsets/extras or infer trust from receipt pins. Result version must be V2 for
the new generation, V1 for immutable04 history. Old rows/artifacts must not be
re-labeled; existing stored original04 remains separate/readable under its
original binding or UNKNOWN under current binding, never counterfeit V2.
Main final binding/bridge source SHA belongs in the actual producer receipt;
this source packet does not pretend to know its future integration commit/hash.

## One batched functional selection (Main only, all UNRUN)

New test_atr_simulation_firstbar_c1_two_groups.py:20 expanded CONTROLLED cases.
Includes both modes LNKS1.64/GLND3.80 geometry, BBCI currentprint stop3.87,
oddlot/definitely excluded and UNKNOWN conditions, causal stop without future
rebound confirmation, exact1/subdollar ticks, first-clock/X3 after partial,
SELL/session precedence, no broker package import, byte mutation, connected
shared-volume conservation/version. Prices are declared CONTROLLED mechanics,
not original-case replay or native proof.

Existing successor replay control's SAME prints/partial quantities now expect
21.56 (196*(2.12-2.01)), not old19.698. Comparison CONTROLLED source fields follow
that exact new target. All five original comparison mechanics remain asserted;
one additional actual-engine CONTROLLED2.0101 fill checks21.5404 vs reported21.54
delta+.0004, retaining nonzero uncentized comparison proof rather than weakening
it away. No raw reference/old receipt/old branch test bytes were rewritten.

Expected batch27: new20 + comparison module6 + ONE changed replay node:
test_candidate_connects_to_partial_allocations_and_target_not_full_print_shortcut.
Main selects fresh XML/raw/private temp and performs runtime review/collection;
these counts are static inventory, NOT collection or PASS evidence.

After review+changed controls+Main sourcebinding, Main owns ONE changed-source
173 replay using the SAME pinned inputs/policy/77manifest, fresh output and exact
source. Recompute per-case differences/totals honestly; do not force old provided
totals. Original cause analyzer is deliberately pinned to04/93/63; it must NOT
be run blindly on the successor. A separate exact successor-receipt ledger
binding/update can follow actual new SHA/counts, without re-executing any model.
Full/UI/PG/version compatibility and other remaining qualification gates are
not waived by C1's single successor-study authorization.
