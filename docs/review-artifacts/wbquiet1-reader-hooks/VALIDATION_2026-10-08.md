# WBQUIET1 reader-hook validation

Branch `codex/wbquiet1-reader-hooks-1008`, exact base
`1e15adb03c3758e647d333828bbe3090e24e832e`. Draft candidate only; not READY.
The source fingerprint in `TESTED_SOURCE_2026-10-08.sha256` identifies the frozen
source/test inputs. Publication and hosted-CI results will be appended after real runs.

## Completed and pending

- Final boundary run: **259 passed**, 9.63 seconds, including all 30 isolated
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
- The restarted lane full-unit run is pending, XML
  `/tmp/wbquiet1-reader-hooks-head-unit.xml`, log
  `/tmp/wbquiet1-reader-hooks-head-unit.log`. Source is frozen during this run.
- The parent retained exact-main Git-worktree baseline completed:
  **48 failed / 7,674 passed / 55 skipped**, 606.61 seconds.
  XML `/tmp/five-lane-main-1e15adb0-unit.xml`, log
  `/tmp/five-lane-main-1e15adb0-unit.log`, checkout `/tmp/codex-1008-five-lane-baseline`.
  Parent failed-name SHA256 is
  `e0b9fc7478ff484d88d9882194d3525632c0d2d43353d515b28f63a28464e9a9`
  (sorted `classname::name`, joined with newline, no terminal newline).
  This lane does not duplicate or replace that baseline.
- Hosted CI has not yet run for a published lane head. No global suite parity,
  CI green, reviewer pin, READY, install or activation claim is made.
- Synthetic trace `REPLAY_2026-10-08.json` ran successfully: seven mocked adapter
  invocations, one SHADOW and five following-window reader receipts; one read
  after the window still proceeds without a receipt. Real network pulls: zero.
  No production data was collected and no wire savings are measured. Recorded
  RESERVE1 fixture controls are included in the 259-test boundary run, distinct
  from this synthetic trace. Agent E owns production A/B/C data separately.
- Stable contract was sent to Lane E through the authorized coordination tool.
  It must use actual later reader lines, not relabel historical/synthetic data.

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
