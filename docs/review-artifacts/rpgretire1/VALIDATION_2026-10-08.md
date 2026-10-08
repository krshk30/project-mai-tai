# RPGRETIRE1 validation receipt

Corrected tested source/test commit: `0f918c5dc5ff3a9ae0142903ee15bec519f22d5b`.
Initial broad/full-unit source: `08abeb6f25d58bb7a51e409558a617799f107e5f`.
Branch: `codex/rpgretire1-flag-off-ownership`.
Base: main `1ed10831508e9a251aa3440a973f49c8bb2c18c9` (#1126 merged).
The following receipt commit adds documentation/generated evidence only.
Independent reviewer pin is required; this is a draft, not a merge/deploy ruling.
#1124 remains separate: the user reports two green runs but strict main protection
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
- Diff whitespace check PASS; log-marker isolation PASS (77 literal count
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

The missing baseline failure is
`test_eod_cron_adoption.py::test_current_wrapper_and_installer_remain_executable`.
Its source is unchanged; the full-run discrepancy is retained rather than
claimed as a fix. The corrected source has **not** had a second full-unit run.
The user's 08:18 ET direction prioritized basic guards/mutations and publishing
the draft ASAP; another full suite does not block publication or reviewer access.

| Receipt | SHA256 |
|---|---|
| Own baseline XML | `193f4e21ca82fcd8244a842696b479e51e0dd50f5e80c49188e77801f8fb9390` |
| Initial source XML | `4b5fe713bc3a1ad8ffa29ba100f2c233ad8eba9e435f82cb5e21db8fd1b0ec9a` |
| Baseline failed-name set | `576b05cb64b27393bb38844a6ca1b2ff8146d8b3b19420016323cfe00a271c15` |
| Initial source failed-name set | `40b1dffce55f3df474ea10e115c64bd0582d16696693b242cdad6d38b3dec552` |

The baseline adds a three-second process-spawn timeout in the unchanged
`test_real_dead_consumer_cannot_block_the_paced_producer` to the 47 previously
recorded failures. Isolated retests pass on both own baseline (1.06 seconds)
and frozen source (1.07 seconds). The original baseline counts above are retained.

## Reproduction and boundaries

Interpreter: `/Users/velkris/Projects/project-mai-tai/.venv/bin/python`,
CPython 3.12.13, pytest 8.4.2, pytest-asyncio 0.26.0.
Exact installed package versions: `DEPENDENCIES_2026-10-08.txt`.
Corrected focused command (raw result: `CORRECTED_BOUNDARIES_2026-10-08.log`):

```text
python -m pytest tests/unit/test_rpgretire1_flag_off.py tests/unit/test_line_chart_restoration_integration.py tests/unit/test_orb_paper_boundary.py tests/unit/test_paper_exit.py -q
```

Both full runs use `PYTHONPATH=src` and:

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
