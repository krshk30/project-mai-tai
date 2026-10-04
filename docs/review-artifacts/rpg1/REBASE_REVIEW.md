# RPG1 current-main rebase and review response

## Scope and authority

Rebased only `codex/rpg1-resting-reprice-gap` from reviewed
`6f3f000c5df611d6d41c3030d810286292c30579` onto immutable main
`250ab18458f4d806aa8bcdf98d787fff5bb57df4`, as authorized on 2026-10-03.
No production write, merge, installation, timer change or #1088 modification.
No repository or ancestor `AGENTS.md` exists; repository/test guidance was read.

The actual findings are in
[claude-1's exact review block](https://github.com/krshk30/project-mai-tai/pull/1085#issuecomment-5974643527).
They were initially absent from GitHub; no missing finding was invented. The
operator's final card removes the one-step swap sentence and explicitly says
partial fills retain the filled shares without buying the remainder again.

## Findings

| Item | Change and falsifier |
| --- | --- |
| P3 | Directly assert the v2 authorization verdict at configured start/end and RTH boundaries, before calling OMS. Removing that v2 guard fails even though OMS has its own final guard. |
| P4 | Two durable jobs are `submitting`. A controlled dispatch-ownership change after the initial entry check makes the replacement token differ from `_rpg_dispatch_token`. The actual OMS final guard refuses before wire. Removing precisely the reviewer's token comparison fails both broker cases. Earlier guards are not disabled or used to mask this boundary. |
| P8 | `_result()` only creates `cancelled_empty` for terminal cancellation; that decoder invariant is documented beside `can_replace`. The dataclass remains independently constructible, so a labelled inconsistent value derived from each recorded result proves the defensive `terminal_cancel` check independently. Removing it fails both cases. This is not presented as a recorded broker answer. |
| F1 | Both v2 and OMS use shared `entry_gate.within_rth_entry_window`, which composes the configured `within_entry_window` with RTH. No RPG-specific 15:45 literal remains. The configured start, configured end, weekend/holiday predicate and RTH intersection govern; final wire is checked after awaits/processing too. |
| F2 | Rebase resolves OMS mixin/pre-wire-claim conflicts by retaining both RPG and NFQ paths. The add/add composition conflict retains real RPG runtime handoff plus all four recorded NFQ fixture cases. Conditional NFQ import skips are removed from both RPG composition modules. |
| F3 | Rebased full-unit failed-name set is compared to the retained untouched 250 baseline, then the exact pushed head runs Validate CI. Results below. |

Configured-window tests cover 09:59 before a configured 10:00 start, exactly
10:00, admission at 15:50 when configured through 16:30, and rejection at 16:00
because RTH has ended. A 500 ms processing delay crosses a configured 14:00 end
while authorization is still younger than one second; neither broker may wire.
Existing 15:45 regression cases now explicitly configure 15:45 rather than
depending on a hidden RPG constant. The four configured fields are
`strategy_schwab_1m_v2_entry_window_{start,end}_{hour,minute}_et`.

The initial extra-live-order investigation found that existing OMS resting-order
dedup already refuses a replacement when another live BUY exists behind a
different generation/price filter. Four tests preserve that backstop; no new
candidate-selection rule was introduced or attributed to P4. Initial expectations
of an earlier `held_unknown` ending were too strict, not production defects.
An initial P4 fixture nested a SQLite session inside the OMS transaction; its
`StaleDataError` is not RED evidence. The final test uses that transaction's own
session, passes unmutated, and fails only with the dispatch check removed.

## RPG switch and rollback

Actual setting: `strategy_schwab_1m_v2_atr_reprice_handoff_enabled`.
Environment: `MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED=true`.
Default is **false**; expected-flags catalog is **true** for the operator-approved
joint install. Effective reader/owner: **schwab-1m-v2**, in
`SchwabV2Strategy._queue_resting_cancel`, for admission of new reprice tickets.
This is new review scope, not an existing flag or a claimed independent pin.

- OFF for a new first/reclaim reprice uses the legacy cancel then next-pass path.
- Already emitted tagged cancels are still recognized by OMS after OFF, including
  a cancel queued before the switch changed. Do not reinterpret them as legacy.
- Durable pending/unknown jobs retain ownership after OFF and cache/retry-object
  restart; regular v2 entries cannot sneak around them. Already-owned work may
  drain to one replacement, original-fill accounting, expiry or safe blocked state,
  always using current gates and durable claim before wire.
- OMS and the v2 feedback callback deliberately do **not** gate recovery/draining
  on this flag. There is no OMS-only RPG environment toggle to invent. A code
  rollback that removes the runtime is different from switching admission OFF:
  it must not proceed while unresolved durable jobs exist.
- This flag controls piece-3 handoff admission, not the already-built first-entry
  stale-quote hold. No shared quote-age, threshold, price/size or exit change.

For the joint plan: restart OMS and v2 with the reviewed code, and apply FLAGGATE
to the new flag in the v2 process. Existing v2 CW-v2/resting, dual-broker fanout,
Webull resting-mirror, reactive/reclaim configuration and NFQ
`oms_v2_webull_mirror_fresh_price_enabled` retain their independently approved
catalog values. Do not turn on the currently disabled reclaim flag as part of RPG.
The new setting does not change numeric values or add a numeric catalog entry.
#1088 adds a separate boolean catalog row; a later joint integration must preserve
both rows and revalidate the exact catalog coverage test/count.

## Verification

Logs and JUnit evidence are retained under `ops/local/rpg14-review/` in this
worktree. Initial configured-window RED had six real boundary failures; four
extra failures came from the overly strict dedup ending expectation discussed
above. The OFF-admission RED had two genuine failures before gating was added.

Final guard/switch suite: **25 passed**. Six review mutations are independently
RED: P3, P4, P8, OMS final configured-window check, OFF admission ignored, and OFF
abandoning an in-flight cancel. Mutation children do not modify tracked source.

Frozen final verification, approved Python with `PYTHONPATH=src` and no bytecode
or pytest cache writes:

- Full units: **5,303 passed, 56 failed, zero skipped**, 312 warnings, 223.46 s
  (`rebased-final-unit.txt/xml`). Untouched 250 baseline is **5,050 passed, 56
  failed**. Exact failed-name sets match: **zero added and zero removed**.
- Broad focused set: **907 passed, zero skipped**, 19.72 s
  (`rebased-final-focused.txt/xml`), including RPG, v2, NFQ, sizing/fanout,
  expected flags, shared entry gate, backtest replay and all three composition
  files. The actual composition subset has **12 passing cases**, no skips.
- All **39 mutations RED** on this rebased source: six exact-review/window/switch,
  nine runtime, five piece-3, eight strict-readback, eight coordinator and three
  first-entry quote-wait mutations. P4's removed check demonstrably reaches one
  simulated wire on each broker and fails `assert not h.adapter.opens`; no fixture
  exception is counted. Full assertion output is retained for the six new cases.
- Full source/test Ruff and whitespace checks pass. The prior 57-failure full run
  and 906-pass/one-failure focused run omitted RPG opt-in in one older partial-fill
  sizing fixture. That fixture now explicitly opts in; these final runs supersede
  those intermediate results. No production fallback or test exemption was added.

Artifact SHA-256 under `ops/local/rpg14-review/`:

| Artifact | SHA-256 |
| --- | --- |
| `rebased-final-unit.xml` | `86ae16be870e558e4593e92f5847c5f937a7934205fda4ca15ff572607b020a7` |
| `rebased-final-focused.xml` | `f5fd447e5da716bcbb58495cf5027652c8d273ec3ed6ee257da7b88161821a53` |
| `rebased-failed-name-comparison.json` | `f0d128424b52f3f1c7b0177c46397b12f5c857e25d14cdaf38f37a3a906b81d8` |
| `review-mutations-assertions.txt` | `27f04120805f1460701f655d7f4c6c6057abe6e13624dad0cbb2ca45b58cb151` |

Reproduce focused guard coverage with `python -B -m pytest -q -p no:cacheprovider
tests/unit/test_rpg1_review_guards.py`; run
`docs/review-artifacts/rpg1/check_review_mutations.py` with the approved interpreter
for the six exact-review/window/switch falsifiers. Full units use the same pytest
options with `tests/unit --junitxml=<scratch-output>`. Exact pushed-head Validate
CI is reported on the PR, separately from the known local-platform baseline.

## Changed files

The rebase conflict resolutions are `src/project_mai_tai/oms/service.py` (both
mixins and claims) and `tests/unit/test_rpg1_nfq1_composition.py` (actual runtime
composition, not the old next-bar seam). The follow-up modifies these 18 files:

- `docs/review-artifacts/rpg1/PIECE3_14_SCENARIOS.md`
- `docs/review-artifacts/rpg1/REBASE_REVIEW.md`
- `docs/review-artifacts/rpg1/check_review_mutations.py`
- `ops/health/expected_flags.json`
- `src/project_mai_tai/broker_adapters/atr_buy_readback.py`
- `src/project_mai_tai/oms/atr_reprice_runtime.py`
- `src/project_mai_tai/settings.py`
- `src/project_mai_tai/strategy_core/entry_gate.py`
- `src/project_mai_tai/strategy_core/schwab_1m_v2.py`
- `tests/unit/test_expected_flags_check.py`
- `tests/unit/test_rpg1_nfq1_composition.py`
- `tests/unit/test_rpg1_review_guards.py`
- `tests/unit/test_rpg1_runtime.py`
- `tests/unit/test_rpg1_runtime_nfq.py`
- `tests/unit/test_schwab_1m_v2_resting_entry.py`
- `tests/unit/test_schwab_1m_v2_resting_orphan.py`
- `tests/unit/test_v2_dual_broker_fanout.py`
- `tests/unit/test_v2_entry_sizing.py`

## Remaining boundary

Live cancel-to-accept latency and native child scaling remain unmeasured. Recorded
parent responses anchor strict reads; acceptance, races and clock movement remain
explicit simulators. Independent new-head review/pin is required before any merge
or installation; no live-evidence claim or deployment authorization is inferred.
