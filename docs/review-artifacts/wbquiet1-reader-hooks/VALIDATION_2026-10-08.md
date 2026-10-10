# WBQUIET1 reader-hook validation

Branch `codex/wbquiet1-reader-hooks-1008`, exact base
`1e15adb03c3758e647d333828bbe3090e24e832e`. Draft candidate only; not READY.
Frozen source/test head: `6d38735b80f651d366556c8f3b5a0310c6ebfaa3`, draft PR #1132.
The fingerprint in `TESTED_SOURCE_2026-10-08.sha256` matches the completed full run
and the unchanged source/test files. This subsequent receipt is docs-only.

## Completed and pending

- Final boundary run: **259 passed**, 9.63 seconds, including all 26 isolated
  reader cases and the original shadow, RESERVE1, stand-down, phantom reconcile
  and ORB controls. Raw `BOUNDARIES_2026-10-08.log`/`.xml` retained.
- Final **19/19 mutations RED**, all from actual assertion failures without
  collection errors. Raw logs/XML and source/log hashes are retained under
  `mutations/`. A prior run's overflow-loss mutant survived because unknown
  acquisition masked that condition; the test now uses a measured acquisition
  and the same guard-removal mutant is RED. No assertion was relaxed.
- Ruff across `src tests docs/review-artifacts/wbquiet1-reader-hooks` passed.
- Three exploratory lane full runs were interrupted while finalizing acquisition,
  loss-test and stale-generation evidence. Their partial logs are
  `/tmp/wbquiet1-reader-hooks-head-unit-interrupted.log`,
  `/tmp/wbquiet1-reader-hooks-head-unit-interrupted-loss-test.log` and
  `/tmp/wbquiet1-reader-hooks-head-unit-interrupted-stale-generation.log`.
  They are not completed receipts and cannot establish failure parity.
- The final frozen-source full-unit run completed: **48 failed / 7,700 passed /
  55 skipped**, 620.66 seconds. Raw `FULL_UNIT_HEAD_2026-10-08.log`/`.xml` retained;
  original paths `/tmp/wbquiet1-reader-hooks-head-unit.log`/`.xml`. All 26 new
  reader cases passed. This is not a local full-suite PASS.
- The parent retained exact-main Git-worktree baseline completed:
  **48 failed / 7,674 passed / 55 skipped**, 606.61 seconds.
  XML `/tmp/five-lane-main-1e15adb0-unit.xml`, log
  `/tmp/five-lane-main-1e15adb0-unit.log`, checkout `/tmp/codex-1008-five-lane-baseline`.
  Parent failed-name SHA256 is
  `e0b9fc7478ff484d88d9882194d3525632c0d2d43353d515b28f63a28464e9a9`
  (sorted `classname::name`, joined with newline, no terminal newline).
  This lane does not duplicate or replace that baseline.
- Literal comparison, both 48-name sets and XML hashes are retained in
  `FULL_UNIT_COMPARISON_2026-10-08.json`. **Sets are not identical**: one added
  failure and one missing baseline failure; 47 names match. No global parity claim.
- Source-head push Validate [37783874613](https://github.com/krshk30/project-mai-tai/actions/runs/37783874613)
  and PR Validate [37783928375](https://github.com/krshk30/project-mai-tai/actions/runs/37783928375)
  both completed green. PR CI: **7,748 passed / 55 skipped** unit, 778.17 seconds;
  **86 passed / 1 xfailed** integration/replay/backtest, 20.62 seconds; log-marker
  isolation and Ruff passed. Hosted CI uses its own environment, not a replacement
  for the local retained baseline comparison.
- The docs-only successor must receive its own hosted checks; source-head green
  is not a claim of successor-head CI green. Independent-review-pin
  [37783928933](https://github.com/krshk30/project-mai-tai/actions/runs/37783928933)
  failed because no reviewer pin is present. No pin authored here; not READY,
  mergeable, install-approved or activated.
- Synthetic trace `REPLAY_2026-10-08.json` ran successfully: seven mocked adapter
  invocations, one SHADOW and five following-window reader receipts; one read
  after the window still proceeds without a receipt. Real network pulls: zero.
  No production data was collected and no wire savings are measured. Recorded
  RESERVE1 fixture controls are included in the 259-test boundary run, distinct
  from this synthetic trace. Agent E owns production A/B/C data separately.
- Stable contract was sent to Lane E through the authorized coordination tool.
  It must use actual later reader lines, not relabel historical/synthetic data.
  Schema SHA256: `1a422dd220a11fa07e2ff1ded30c6eaac8079b944c9e5cc1143e69ed6ba4e2d8`.

## Literal failure difference

Added on head:

```text
tests.unit.test_hotfix1_symbol_tick_cache::test_recorded_240_events_per_second_60_seconds_slow_db_flat_transactions[retained-on]
```

Missing from head (failed in baseline):

```text
tests.unit.test_hotfix1_symbol_tick_cache::test_recorded_240_events_per_second_60_seconds_slow_db_flat_transactions[retained-off-existing-row]
```

Head failed-name SHA256:
`3855c20d037bd9064c8755a06d4039d513b57a2f3b0118a6da833b689199e912`.
The same sorted/no-terminal-newline convention applies to both sets.
Original full-run loop-stall measurements against the unchanged 50 ms bound:
baseline retained-off-existing-row **52.597541973227635 ms**; head retained-on
**179.77112025255337 ms**. Both processed 14,400 recorded events in approximately
60 seconds with zero buys queued. Retained-on recorded one DB transaction;
retained-off-existing-row zero. These offline fixture measurements do not prove
wire savings, downstream decision equivalence or the cause of the timing swap.
The producer spawn-timeout control also failed in both original full runs.

## Independent delta retests

After both full runs completed, the added and missing timing names plus
`tests.unit.test_momentum_gateway_throughput::test_real_dead_consumer_cannot_block_the_paced_producer`
were retested independently on each checkout, sequentially:

- Exact retained baseline: **3 passed**, 121.95 seconds;
  `DELTA_BASELINE_2026-10-08.log`/`.xml`.
- Frozen source head: **3 passed**, 121.86 seconds;
  `DELTA_HEAD_2026-10-08.log`/`.xml`.

Both retained tick cases kept the original 50 ms loop-stall assertion, and the
producer kept its original three-second spawn bound. No assertion, fixture or
source was weakened. The import paths were verified to the two separate Git
worktrees, using the same shared Python 3.12.13 interpreter and normalized PATH;
baseline PYTHONPATH was `/tmp/codex-1008-five-lane-baseline/src`, head PYTHONPATH
was this lane's absolute `src`. These selected passes are consistent with timing
sensitivity, not causal proof and not replacements for the original full results.
No local test process remains running.

## Frozen source hashes

```text
fc0dab43141c40238595efe6f333f3de9b23b44f9d1a8dce71eb4b0833d32887  src/project_mai_tai/oms/service.py
83cf4fbb613171d51ebc86141ecb1910457cd0c1cb2e43aa7399a8e00e23ce17  src/project_mai_tai/oms/wbquiet_shadow.py
c37f46ff9057cd5e2f5243a112ae59b5c58935286ce8e3116b38278883ff7480  src/project_mai_tai/oms/orb_schwab_eod.py
c4b4b7be5fd16c5630ceb8f94d35a513bc9f3ac5fff8e942e5df346a662e4475  tests/unit/test_wbquiet_shadow_readers.py
```

## Reproduction

Interpreter: `/Users/velkris/Projects/project-mai-tai/.venv/bin/python`.
Import resolved to this exclusive worktree's `src/project_mai_tai/oms/wbquiet_shadow.py`.
Exact package versions are retained in `DEPENDENCIES_2026-10-08.txt`.

```text
PATH=/opt/homebrew/bin:/Users/velkris/Projects/project-mai-tai/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin
PYTHONPATH=/Users/velkris/.codex/worktrees/wbquiet1-reader-hooks-1008/project-mai-tai/src
python -m pytest tests/unit/test_wbquiet_shadow.py tests/unit/test_wbquiet_shadow_readers.py tests/unit/test_reserve1.py tests/unit/test_oms_native_oco_stand_down.py tests/unit/test_oms_v2_phantom_reconcile.py tests/unit/test_orb_schwab_order_route.py tests/unit/test_orb_schwab_exits.py -q --junitxml=<boundary.xml>
python -m pytest tests/unit -q --junitxml=<head.xml>
python docs/review-artifacts/wbquiet1-reader-hooks/mutate.py <temporary-output>
python docs/review-artifacts/wbquiet1-reader-hooks/replay.py
ruff check src tests docs/review-artifacts/wbquiet1-reader-hooks
```

No installation or dependency changes were made. See Step 0 for reader inventory,
memory/latency bounds, missing-evidence rules and production restrictions.
