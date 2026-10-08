# RPGRETIRE1 validation receipt

Corrected tested source/test commit: `0f918c5dc5ff3a9ae0142903ee15bec519f22d5b`.
Corrected full-unit tested head: `333c8756bb2df0c1c96c5de449e56037b0cb7a3f`.
Its source/test trees match the corrected source/test commit exactly.
Initial broad/full-unit source: `08abeb6f25d58bb7a51e409558a617799f107e5f`.
Branch: `codex/rpgretire1-flag-off-ownership`.
Base: main `1ed10831508e9a251aa3440a973f49c8bb2c18c9` (#1126 merged).
The following receipt commit adds documentation/generated evidence only.
Independent reviewer pin is required; this is a draft, not a merge/deploy ruling.
#1124 remains separate: the parent coordinator reports two green runs but strict main protection
marks it BEHIND, with merge pending an operator ruling. It is not a dependency
included in this sidecar or a validation result borrowed for this change.

## Completed checks

- Corrected source: **172 boundary/line/ORB/paper tests passed** (7.01 seconds),
  including the 45 new OFF/ON regressions and all five initially added failures.
- Initial source's new OFF/ON regressions: **45 passed** (6.46 seconds).
- Paired startup controls: **143 passed** (21.77 seconds).
- Broad RPG1/RPGSTUCK1, GAPLINE1, never-synthesize-flat, held exit coverage,
  list-primary and flip ownership controls: **782 passed, 55 skipped**
  (100.88 seconds). The historical unmeasured skips remain skips.
- RPG1/NFQ1 composition: **4 passed** (0.83 seconds).
- Ruff 0.15.22: `ruff check src tests docs/review-artifacts/rpgretire1` PASS.
- Authored source/test/document whitespace check PASS; raw generated log/XML
  trailing whitespace is retained verbatim, not normalized. Log-marker isolation
  PASS (77 literal count
  consumers, 394 emitted markers, 1,026 logging formats).
- **15/15 guard-removal mutations RED** on corrected-source temporary copies. Each
  mutant ran the 45 new regressions; its actual assertion failures and raw
  logs are retained in `mutations/summary.json` and `mutations/*.log`.

Mutants remove OMS open refusal, old-order ownership, external token ownership,
cancel begin/routing, stale tick advance, retry startup/switch-off/scanning,
evidence scanning, v2 full/cached restore, authorization, cached entry ownership,
refused-leg recovery and placement cache guards. No mutant edits the writer tree.

## Full-unit comparison and correction

Own untouched baseline (git archive of `1ed10831`): **48 failed, 7,585 passed,
55 skipped**, 624.67 seconds. Raw log/XML: `/tmp/rpgretire1-baseline-unit.log`
and `/tmp/rpgretire1-baseline-unit.xml`.

The initial-source full-unit run completed: **52 failed, 7,630 passed, 55 skipped**,
621.29 seconds. Raw output: `/tmp/rpgretire1-final-unit.log` and
`/tmp/rpgretire1-final-unit.xml`. The complete failed-name comparison is retained
in `FULL_UNIT_INITIAL_COMPARISON_2026-10-08.json`: five added failures and one
missing baseline failure. No full-unit PASS or failure-name equivalence is claimed.

Four added failures were dependency-free ORB/Polygon refusal controls: the new
OMS flag helper touched absent `settings` before their existing refusal boundary.
The correction makes absent settings default OFF. The fifth was a line-chart
authorization control still configured OFF; it now explicitly enables RPG so it
continues to prove ON's current-line refusal. All five pass in the corrected
172-test run; no inherited failure was changed to make it pass.

The corrected-source full-unit run completed without source changes:
**47 failed, 7,635 passed, 55 skipped**, 613.02 seconds. Raw log/XML:
`/tmp/rpgretire1-corrected-full-unit.log` and
`/tmp/rpgretire1-corrected-full-unit.xml`. The retained baseline was not rerun
or replaced. `FULL_UNIT_CORRECTED_COMPARISON_2026-10-08.json` records the exact
comparison: **zero added failure names, one missing baseline failure**.
`compare.py` exits 1 for that missing name; no full-unit PASS or strict
failure-name equivalence is claimed.

The sole missing baseline failure is
`test_eod_cron_adoption.py::test_current_wrapper_and_installer_remain_executable`.
The original archive has no Git metadata, so its unchanged `git ls-files --stage`
assertion fails with exit 128. This is not a source fix. Separate unchanged
assertions pass on the baseline when supplied with a temporary index populated
from `1ed10831`: **1 passed**, 0.08 seconds. Only the temporary index was written;
no existing index, branch or baseline source was modified.

The 08:18 ET publication/priority instruction came from the **parent coordinator,
not a human/operator direction or ruling**. It is not permission to bypass quality
checks. This corrected full run and both-head retests are completed before this
docs-only receipt; the draft remains not READY while CI is red or pending.

| Receipt | SHA256 |
|---|---|
| Own baseline XML | `193f4e21ca82fcd8244a842696b479e51e0dd50f5e80c49188e77801f8fb9390` |
| Initial source XML | `4b5fe713bc3a1ad8ffa29ba100f2c233ad8eba9e435f82cb5e21db8fd1b0ec9a` |
| Corrected source XML | `52a74cac58745667b5447ea352d41c8a83860091ba07117fe64abaf07e7990c9` |
| Baseline failed-name set | `576b05cb64b27393bb38844a6ca1b2ff8146d8b3b19420016323cfe00a271c15` |
| Initial source failed-name set | `40b1dffce55f3df474ea10e115c64bd0582d16696693b242cdad6d38b3dec552` |
| Corrected source failed-name set | `fe24b2de3074d47d6104576c5d4392a4ac85e0fda55b246755703dc903859c50` |

## Independent retests and hosted CI

Seven cases were rerun independently on both trees: the five initially added
ORB/Polygon/current-line failures, the missing EOD case and the unchanged
`test_real_dead_consumer_cannot_block_the_paced_producer` timeout control.
The archive result is **6 passed, 1 failed** (EOD metadata only), 1.66 seconds;
corrected head is **7 passed**, 1.69 seconds. Raw logs/XML are retained under
`retests/`; no assertion or timeout was relaxed. The separate baseline EOD index
control supplies missing repository metadata without changing the test.

The producer timeout fails in both full runs but passes in both independent
retests. Earlier standalone checks also passed (baseline 1.06 seconds, head 1.07
seconds). Preserve this measured full-vs-isolated context difference. It was
already present in the historical 47-failure GAPLINE receipt; the baseline-only
difference from that receipt is EOD metadata, not this timeout.

Hosted Validate at `333c8756` is **red**, not green:

- Push run `37776844142`: **1 failed, 7,681 passed, 55 skipped**, 725.94 seconds.
- PR run `37776850263`: **1 failed, 7,681 passed, 55 skipped**, 776.12 seconds.
- Both fail only
  `test_nfq2_tick_path.py::test_nfq2_slow_save_does_not_stall_loop_and_concurrent_quotes_queue_once`.
  Measured elapsed times are 0.970260577 and 0.837468318 seconds against the
  unchanged 0.05-second bound. Hosted Python is 3.12.14; local Python is 3.12.13.
- The same unchanged timing control passes in separate local baseline and
  corrected-head retests (**1 passed** in 0.97 and 0.89 seconds respectively),
  retained under `retests/`. This does not establish a
  hosted baseline result or prove the hosted failure is harmless.
- Integration/replay/backtest and hosted Ruff were skipped after unit failure.
  Local Ruff passed; no hosted all-green claim is made.
- `independent-review-pin` run `37776846822` also fails: no independent pin.
  No pin is authored here. Raw Validate failure logs are retained under `ci/`.

The following docs-only push will trigger new CI. Until those checks are green
and the independent reviewer supplies the required pin, PR #1128 stays a draft
and is **not READY**. No source widening is justified by these results.

## Reproduction and boundaries

Interpreter: `/Users/velkris/Projects/project-mai-tai/.venv/bin/python`,
CPython 3.12.13, pytest 8.4.2, pytest-asyncio 0.26.0.
Exact installed package versions: `DEPENDENCIES_2026-10-08.txt`.
Corrected focused command (raw result: `CORRECTED_BOUNDARIES_2026-10-08.log`):

```text
python -m pytest tests/unit/test_rpgretire1_flag_off.py tests/unit/test_line_chart_restoration_integration.py tests/unit/test_orb_paper_boundary.py tests/unit/test_paper_exit.py -q
```

Independent seven-case retest command, run once per tree with the same environment:

```text
python -m pytest tests/unit/test_line_chart_restoration_integration.py::test_rpg_authorization_refuses_replacement_without_current_line tests/unit/test_orb_paper_boundary.py::test_manual_orb_intent_is_refused_by_oms_before_any_dependency_is_touched tests/unit/test_paper_exit.py::test_oms_refuses_polygon_intent_before_database_or_broker tests/unit/test_eod_cron_adoption.py::test_current_wrapper_and_installer_remain_executable tests/unit/test_momentum_gateway_throughput.py::test_real_dead_consumer_cannot_block_the_paced_producer -q --junitxml=<receipt.xml>
python -m pytest tests/unit/test_nfq2_tick_path.py::test_nfq2_slow_save_does_not_stall_loop_and_concurrent_quotes_queue_once -q --junitxml=<receipt.xml>
```

Baseline EOD metadata control uses `GIT_INDEX_FILE=/tmp/rpgretire1-baseline-mode.index`,
`GIT_WORK_TREE=/tmp/rpgretire1-baseline.7O1kTp` and
`GIT_DIR=/Users/velkris/Projects/project-mai-tai/.git/worktrees/project-mai-tai69`.
`git read-tree 1ed10831508e9a251aa3440a973f49c8bb2c18c9` populates only that
temporary index; pytest then runs only the EOD case with these variables.
Both indexed scripts are mode `100755`; assertions remain unchanged.

All three full runs use `PYTHONPATH=src` and:

```text
PATH=/opt/homebrew/bin:/Users/velkris/Projects/project-mai-tai/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin
python -m pytest tests/unit -q --junitxml=<receipt.xml>
```

`python` resolves to the same 3.12.13 environment; shell `python3` resolves to
`/opt/homebrew/bin/python3`; bash resolves to `/bin/bash`. Own baseline directory:
`/tmp/rpgretire1-baseline.7O1kTp`. Sidecar imports were verified to resolve to
the designated worktree, not the primary checkout. `compare.py` compares completed
full-suite XML failure names without suppressing differences.

Source edits are limited to four files: OMS runtime and cancel routing, v2 bot
handoff pass and v2 cached ownership. Six test files update the superseded OFF
restore expectation and add paired controls. See `STEP_0_2026-10-08.md` for the
complete caller inventory and evidence limits.

No journal implementation, handoff manifest, production DB/archive, service,
broker, existing branch, deployment or merge was changed. Current122/AIXI 07:50
is user-reported context; the reviewer already performed its archive. Source
tests use isolated SQLite and simulated acceptance, not a production replay.
