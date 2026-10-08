# GAPLINE1 validation receipt

Frozen source/test head: `d694b85d084bdccdb36911e30a6d89329ff21217`.
As of 2026-10-08 07:16 ET. No production action.

## Final-source mutations

Each mutant is a separate temporary source copy, imported before pytest so the
test conftest cannot accidentally select the unmutated checkout. Tests:
`test_gapline1.py` and `test_gapline1_recorded.py`. Each run retains the 55
UNMEASURED skips; failures below are assertions against the mutated source.

| Mutation | Failed | Passed | Result | Raw receipt |
|---|---:|---:|---|---|
| Restore initial-gap indicator reset | 65 | 30 | RED | `/tmp/gapline1-final-mut-initial.log` |
| Restore recovery-gap indicator reset | 22 | 73 | RED | `/tmp/gapline1-final-mut-recovery.log` |
| Remove ten-bar wait (require one bar) | 6 | 89 | RED | `/tmp/gapline1-final-mut-wait.log` |
| Seed carried initial state as long | 28 | 67 | RED | `/tmp/gapline1-final-mut-long.log` |
| Skip pending state-only carry advance | 2 | 93 | RED | `/tmp/gapline1-final-mut-carry.log` |

Independent read-only review retested the late-interior-source refresh and
exit-covered repair fixes on this head: 97 nearby controls PASS; no further
actionable finding. This is not the reviewer's production pin.

## Full-suite pairing

Main `d244f602491e5d316a05670b205315466aed2a4d`: 47 failed / 7,491 passed,
632.94 seconds; `/tmp/gapline1-main-unit.log` and `.xml`.
Frozen head: 47 failed / 7,586 passed / 55 skipped, 613.04 seconds;
`/tmp/gapline1-final-unit.log` and `.xml`. Added failed names: none; missing
baseline failed names: none. Sorted failed-name set SHA256:
`17d68105856e998ab629144135fd40b4ec5b3b4a53fe197c069a0cc1f25eb419`.
As of 2026-10-08 07:21 ET. Focused final run: 230 passed / 55 skipped;
four original gap/seed controls files: 34 passed. Ruff and diff checks PASS.

| Receipt | SHA256 |
|---|---|
| Main unit log | `81200ea439744d801329782c7348264d61c5d43ed0dba0ac844f691dd74840fd` |
| Main unit XML | `6f22841cbd6ee4f4fd4cf5901b060a5c26f7f1cc63b1b29ffdfee21ae228afe9` |
| Head unit log | `5582fd7caa4a85009136c1f01640fb78568af763753bb9453a3c7a04212462c7` |
| Head unit XML | `d0190426c0fad3d4fc785cf9cfd4e78b4430e2d338785ae2b667d6aa2fc394b3` |
| Head focused log | `2ca5716afebce1073f1b0016850c909052c1f58019df3d85166cc35e7b9fe92f` |
Both use the same Python environment and explicit Homebrew-first subprocess
PATH. An earlier run was invalidated by concurrent source edits and is NOT
the final comparison. An earlier worktree run also selected system Python 3.9
for shell helpers; no test is changed to accommodate that environment error.

This is a failed-name-equivalent full suite, not an all-green main or a complete
historical-population PASS. Hosted Validate remains pending at this receipt;
independent-review-pin has no record yet. The receipt commit changes docs only.
