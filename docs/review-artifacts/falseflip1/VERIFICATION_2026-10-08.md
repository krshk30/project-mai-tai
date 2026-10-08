# FALSEFLIP1 Classification-Only Draft Checkpoint

Status: **NOT H1-H4 runtime-ready. NOT pin/merge/install-ready.**
Base: `1e15adb03c3758e647d333828bbe3090e24e832e`.
Writer: codex-2, isolated `codex/falseflip1-entry-close-budget-1008`.

## Measured Controls

Own source import path points at this worktree, not another lane's source.
`test_falseflip1_step0.py`: **76 passed**. Together with existing
`test_v2_retry_one.py` and `test_retryleft1.py`: **144 passed in 1.40 seconds**.
The composition XML is retained beside this receipt. Ruff: all checks passed.

- Seventeen recorded false opportunities / 24 filled legs, scalar bot close and
  trail from the actual fill minute. The cohort contains 90 buy legs / 61 slots.
- Eight exact bound false entries classify FALSE_FLIP. Sixteen historical legs
  with unprovable durable entry bindings stay UNKNOWN to the runtime classifier.
- Five exact bound real inputs classify REAL_FLIP; five corresponding controlled
  current-main owner/close/place runs with retry enabled and max_retries=0 emit
  no second rest. These are existing-budget controls, not new-runtime H4 PASS.
- Forming bars, replay bars, foreign order/client/account/symbol, missing binding,
  non-finite price, SHORT at/above the line: UNKNOWN, no false-episode proof.
- Labelled synthetic mixed REAL/UNKNOWN siblings, missing sibling and still-held
  sibling: no false-episode close proof. Different entry bars in the same slot
  require both exact closed rows and produce one episode proof, not two counters.
- Existing zero-budget close consumption is reproduced for confirmation,
  CW hard stop and OCO-resolved-flat mechanisms. No execution mechanism is changed.

## Mutations

Eight isolated source-overlay mutants, each run against unchanged classifier
tests: **8/8 assertion RED**. No collection error is accepted as mutation proof.
Complete outputs: `CLASSIFIER_MUTATIONS_2026-10-08.json`.

| Removed/changed boundary | Result |
| --- | --- |
| Exact entry-order match | RED |
| Completed-bar guard | RED |
| Live-not-replay guard | RED |
| SHORT close below trail | RED |
| LONG classification changed to FALSE | RED |
| Every filled sibling required | RED |
| Every exact managed row closed | RED |
| False label preserves mechanism | RED |

These do not test a cancel, restored buy, skip counter, runtime callback, durable
write, page or restart recovery. No such implementation is claimed.

## Known Draft Gap

`test_no_inert_modules.py`: **1 failed / 5 passed**, specifically
`test_no_new_module_is_imported_by_nothing` names `project_mai_tai.falseflip1`.
This accurately detects the intentionally unwired checkpoint. The exemption
lists and the test are unchanged. Hosted Validate is not expected green for this
draft; no review-ready label, pin or merge is requested.

Full-suite pair: **NOT RUN / parity UNMEASURED**, not replaced by focused results.
Parent's baseline receipt `/tmp/five-lane-main-1e15adb0-unit.xml` reports
48 failed / 7,674 passed / 55 skipped, failed-name SHA256
`e0b9fc7478ff484d88d9882194d3525632c0d2d43353d515b28f63a28464e9a9`.
The measured additional inert-module failure means this draft is not suite-parity
ready regardless of that baseline. Full comparison belongs to runtime integration.

## Remaining Decisions And Delivery

H3: after two false episodes and one skipped cross, later same-segment crosses
allowed or blocked until fresh SELL? Parent has asked the operator. No answer is
assumed here; no third-false waiver, timer or tolerance is introduced.

Runtime remains unchanged: no feature flag/catalog entry, managed-row update,
exit-summary/page label, owner refund, counter, skip, normal-rest restoration or
cancel-barrier change. The FLYE page row is **UNMEASURED / not implemented**.
Already-closed FLYE entry classification is established from recorded evidence,
not persisted. Unknown MI and conflicting PFAI probes remain disclosed.

Existing RETRY_ONE_ENABLED=true / MAX_RETRIES=0 semantics are retained. No
production, broker, service, DB, Redis, merge, pin or activation operation occurred.
Parent owns shared handoff. This sole writer resumes runtime work after H3 is ruled.
