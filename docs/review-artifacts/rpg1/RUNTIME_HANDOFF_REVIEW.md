# RPG1 piece 3: runtime handoff

This is the d38a86a4 implementation checkpoint. The subsequent fourteen-scenario
request, exact AMOD 13:56 fixture, unknown-notice fix and current-main verification
are recorded in [PIECE3_14_SCENARIOS.md](PIECE3_14_SCENARIOS.md).

Scope starts at `352da22a4169f7d9d6cfa77e938e2d1004bee008` on
`codex/rpg1-resting-reprice-gap`. No rebase, main merge, broker write, production
change, timer change, or deployment. Draft PR #1085 remains a review boundary.

## Runtime path

Real v2 reprice cancels now carry the old resting generation, entry slot,
economic segment, and a shared cancellation generation. OMS resolves an exact
old BUY, persists ownership, cancels, and immediately performs the dedicated
strict read. A missing/ambiguous parent becomes `held_unknown`, not permission
to buy. Old queued cancellations cannot retarget a newer resting generation.

Webull's normal accepted response may contain only its original client order ID.
For the configured Webull account only, that exact identity can cancel/read;
the fresh detail must match client ID, symbol, side and quantity and contain a
broker ID. The journal binds that broker ID before accounting/replacement.
Schwab still requires its exact known broker ID. Missing/contradictory cumulative
fills never default to zero. The recorded Webull zero/full bodies are unchanged;
only the local acceptance timing is controlled in this regression.

Only terminal cancellation plus explicit cumulative zero clears a broker order.
The cancellation ACK never clears it. Reads retain the existing one-second
spacing, two-second timeout, and thirty-read bound. The background generic
order reader/refresh path yields ownership of this exact parent, including when
a generic GET was already in flight. Other orders keep their existing behavior.

The bot consumes durable feedback and evaluates current first/reclaim gates.
The one-second loop does not require another bar; bar/quote drains also revoke
authorization. OMS requires an authorization no older than one second, checks
the current price/size/identity, persists both the submission claim and pending
client order before wire, and rechecks immediately before submission. The normal
risk, sizing, eligibility-cache, native-bracket, and adapter lane remains in use.
15:45 ET is a hard replacement cutoff even if the configured window is longer.

Positive cumulative fills go through original-OPEN accounting and protection.
Terminal partial cancellation closes only the unfilled remainder; it never
buys a remainder or a fresh full quantity. Accounting failure or missing price
retains ownership. v2 consumes the slot on fill. Restart feedback consults the
committed replacement order, rather than resurrecting a stale `placed` latch.
Uncertain submission cannot be blindly repeated; committed later accounting
can resolve it without another wire action.

NFQ no-wire holds are distinct from broker cancellation evidence. A held/queued
never-submitted generation can transfer ownership transactionally without an
invented cancel response. RPG owns subsequent NFQ/PRICE_AGGRESSIVE waits and
reauthorizes through the serial lane; old NFQ/PA1 tokens cannot bypass it.
Broker refusal budgets, current gates, and per-leg independence are retained.

## Scenario matrix

| BUGS v3 | Runtime evidence and safe state |
| --- | --- |
| 1 Normal, both brokers | Recorded terminal-zero bodies; actual v2 cancel, OMS dispatch/read, bot callback, normal serial submit. One simulated accepted replacement: `placed`. |
| 2 Full fill race | Recorded Webull AMOD full fill, original BUY/managed-position accounting, no replacement: `filled`. |
| 3 Partial race | Explicit controlled 31/197 Schwab and 17/98 Webull variants. One cumulative fill, actual-size managed position/protection, terminal remainder cancelled, no rebuy. |
| 4 Unknown/slow | Actual runtime timeout and repeated unknown delivery, one cancel, one-second spacing, thirty-read bound, restart cannot duplicate; `waiting` then `held_unknown`. |
| 5 Refusals | Retained Webull stop/PRICE_AGGRESSIVE texts and Schwab assessment excerpt projected onto explicit simulator requests. Terminal refusal or bounded `price_wait`, never falsely resting; NFQ exercised in combined source. These texts are not raw broker bodies. |
| 6 Independent legs | Both directions: an unproven leg remains held while the other recorded parent clears and its replacement moves; the healthy leg can still reprice. The real Schwab ineligible cache also refuses serial replacement. Missing-parent and cache setup are controlled, not fabricated broker answers. |
| 7 BUY flip during gap | AMOD 13:56 recorded probe/cancel replay, and 13:57 recorded BUY flip before delayed clearance. Normal case replaces in the 13:56 turn; delayed case expires. |
| 8 15:45 / 16:00 | Current callback and final wire guard reject, including processing delay across cutoff. |
| 9 Liquidity | Current thin bar expires the candidate despite earlier placement eligibility. |
| 10 Restart | Durable cancel/read/clear/submit phases, empty v2 state waits, pending submission never replayed, committed later fill/terminal state overrides stale latch. |
| 11 Caps / sizing | First/reclaim consumption, real RETRY-ONE/owner gates, armed-long reclaim, preserved slot/segment/entry ordinal, current dollar sizing. Native replace/place-first are not used, so their oversize scenario is not applicable. |

`test_rpg1_runtime.py` uses production entry points with recorded strict reads.
Placements, clocks, races, quote delivery, and seeded ledger/state are explicitly
simulated. The AMOD probe supplies timestamp/high/low/close/volume/trail; its open
and quote were not recorded in that probe and are controlled replay inputs.
This is not a claim that a historical broker would have accepted the new order.

The ordinary backtest remains an explicit offline model: cancellation clears
its own synthetic order and replacement remains bar-delayed. It does not model
RPG cancellation latency and must not be used to grade this runtime handoff.

## Verification

Approved Python runtime, `PYTHONPATH=src`, bytecode/cache writes disabled.
Raw logs and JUnit files are in `ops/local/rpg-runtime-review/` in the RPG worktree.

- RED: existing inert-module failure; missing runtime cancel marker; background
  polling bypass of strict ownership; retained-session stale authorization;
  Webull client-ID-only acceptance. All have targeted green regressions.
- Mutations: eight strict-reader, eight coordinator, nine runtime, and three
  first-quote-wait mutations. All 28 fail assertions, not import/anchor errors.
- Final focused run: 174 passed (51 runtime, 81 strict-readback, 36 coordinator,
  six no-inert-module checks), `focused-final-scope.txt`.
- Final combined selection: 586 passed, zero skips, in 16.83 seconds;
  `integration-final-scope.txt` and `integration-final-scope.xml`. Includes all
  three composition modules, NFQ, quote waits, OMS deferred resubmits, first and
  reclaim resting paths, fan-out, sizing, polling and offline replay regressions.
- Scoped Ruff checks and both worktrees' diff whitespace checks pass.
- Fresh frozen b8 baseline: 4,935 passed, 57 failed. Starting-head coordinator
  plus inert test: 41 passed, one expected inert-module failure. The intermediate
  stable runtime run had 5,123 passed, 56 failed, two combined-only skips, with
  no new failed names versus b8. Final-scope results supersede intermediate runs.
- Final full unit suite: **5,139 passed, 56 failed, two combined-only skips,
  312 warnings**, 208.31 seconds. **Zero new failed names versus frozen b8**.
  Logs: `current-unit-final-scope.txt`, `current-unit-final-scope.xml`,
  `failed-name-comparison.json`. The one baseline name absent in this run is
  `tests.unit.test_low_priority_alerts::test_digest_install_plan_guards_both_sources_and_dst_candidates`;
  this is not claimed as an RPG fix. Background protection-task warnings remain.

Final JUnit SHA-256:
`5496044a82566070537da5590d1c5aaae2d23d31e4f6af7b6cf14ab6d55077e9`.
Failed-name comparison SHA-256:
`2e0728c480b9572442f15bf09111bb0f7059815378127055a446cda24c53e644`.

Reproduce the full comparison with the approved runtime and `PYTHONPATH=src`:
run `python -m pytest -q -p no:cacheprovider tests/unit --junitxml=result.xml`
in this checkout and frozen b8, then compare `(classname, name)` for every JUnit
testcase containing `failure` or `error`. Raw failed-name sets are retained,
not inferred from failure totals.

## Combined source

Detached scratch checkout:
`/Users/velkris/.codex/worktrees/rpg-runtime-integration/project-mai-tai`, based on
immutable `608339894a1cfb33284e695196df55c18f312889` (NFQ included). Applied only
b8-to-RPG source delta, not the whole RPG tree. No tracked NFQ branch was edited.

The source overlap is limited to OMS service wiring: retain both imports,
inherit `AtrRepriceRuntimeMixin, MirrorFreshPriceMixin`, and persist the pending
order when either `rpg_handoff_token` or `nfq_retry_token` exists, retaining RPG's
final current-gate checks. Existing NFQ hold/report/serial hooks stay intact.
The combined patch is retained locally as `combined-source.patch` in the evidence
directory. The legacy shared-seam fixture test is migrated in this PR to invoke
the actual runtime handoff; standalone RPG explicitly skips NFQ-only modules.
Integration must retain this PR's migrated
`tests/unit/test_rpg1_nfq1_composition.py` where NFQ added the same path; it keeps
all four recorded NFQ fixture cases and replaces only the old next-bar harness.

Combined-source patch SHA-256:
`ada78d9cba8d6088ff6601ab3871cbbd36f0156dad1fabe2b3a22d57ef28c268`.
This combined test covers the actual RPG runtime delta, not just the original
quote-wait/cancel seam. No native replace or place-first path is implemented.

## Changed files

- Runtime: `oms/atr_reprice_runtime.py`, `oms/atr_reprice_handoff.py`,
  `oms/service.py`, `services/schwab_1m_v2_bot.py`,
  `strategy_core/schwab_1m_v2.py`, `broker_adapters/atr_buy_readback.py`,
  `broker_adapters/webull.py`, and explicit offline `backtest/replay.py`.
  All paths in this item are under `src/project_mai_tai/`.
- New runtime/composition tests: `tests/unit/test_rpg1_runtime.py`,
  `test_rpg1_runtime_nfq.py`, `test_rpg1_nfq1_composition.py`; strict readback
  additions in `test_rpg1_buy_readback.py`; recorded refusal-text fixture in
  `tests/fixtures/rpg1_recorded_refusal_texts.json`.
- Existing expectation migrations: `tests/unit/test_schwab_1m_v2_resting_entry.py`,
  `test_schwab_1m_v2_resting_orphan.py`, `test_v2_dual_broker_fanout.py`,
  `test_v2_entry_sizing.py`; scope clarification only in
  `tests/composition/test_rpg1_nfq1.py`.
- Review evidence: this document, historical `REPRICE_HANDOFF_REVIEW.md`,
  `check_runtime_mutations.py`, and the unique anchor fix in
  `check_first_quote_mutations.py`.

## Evidence limits

No live cancel-to-replacement latency measurement, live partial-race sequence,
or native child-order scaling measurement was made. Recorded historical GET
latencies are not full-operation latencies. Simulated protective orders and
actual-size managed-position accounting do not prove venue child scaling.
Schwab's retained refusal is an assessment excerpt, not a full raw answer.
The unit baseline retains known failures and background-task warnings; it is
not a clean full-suite claim. Merge, deployment, and checkpoint pinning remain
separate operator decisions, not granted by completion of this implementation.
