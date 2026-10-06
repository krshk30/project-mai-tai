# ORBPURPLE1 lane B verification

Base: `3ebde364d4634fdad45992e2ab1cbdf43ffeb221` in a separate untouched
managed worktree. Step 0 AGREE was committed first at
`00a5464422940e8b627cccdcdbfd6463acdefc2a` before implementation.
PR: https://github.com/krshk30/project-mai-tai/pull/1098.

## Exact full UNIT pair

Both runs use the requested command, retaining normal PATH after the venv:

```sh
env PATH=/Users/velkris/Projects/project-mai-tai/.venv/bin:$PATH PYTHONPATH=src:. python -m pytest -q tests/unit
```

Baseline raw stdout: `/tmp/orbpurple1-unit-base.log`.
Baseline: 6416 passed, 47 failed, 0 errors, 6463 cases.
Final head raw stdout: `/tmp/orbpurple1-unit-head-final.log`.
Final head: 6447 passed, 47 failed, 0 errors, 6494 cases (+31 passing cases).
Complete failed-name comparison: `unit-pair.json`; all 47 names match,
both error sets are empty, and no failure or error was added or removed.
Raw comparison: `/tmp/orbpurple1-unit-pair-final.json`.
The tested source and test tree
are unchanged between that run and the final commit; only this evidence
document and derived result artifacts are subsequently added.

## Recorded Cases And Controls

The 559 retained Schwab bars replay all six initial-placement candidates
through the real completed-bar reader, ATR calculation, and producer MACD
path. MI/JAGX pass, APUS withholds; neither filled loss is claimed avoided.
Real database-backed OMS replays exercise MI, JAGX, and APUS. Controls cover
rollback, independent paper defaults, unknown evidence withholding/recovery,
negative MACD, observation mode, stale/forming/unordered/unwarmed bars,
line equality in both ATR states, OMS bypass prevention, and post-preview
ATR revalidation. Final review corrected the zero-range equality edge:
a short state with close equal to trail is not an under-line close.

269 focused entry/exit/routing/paper controls passed
(`/tmp/orbpurple1-controls.log`). 35 OWNMIX1 controls passed. The final
catalog/consumer/default/gate run passed 144 tests
(`/tmp/orbpurple1-catalog-final.log`); these counts overlap, not additive.
All seven targeted in-memory mutations are RED with assertion failures and no
collection/setup errors. See `mutation-results.json`; raw logs and XML:
`/tmp/orbpurple1-mutations-final/`. Reproduce with `check_mutations.py`.
Ruff, log-marker isolation, and whitespace checks passed.

The added independent flag has two consumers (ORB-Schwab and OMS).
Legitimate catalog fixture refresh: 129 -> 130 flags, 141 -> 143 boolean
consumer checks, and 149 -> 151 total checks including eight numeric checks.
No existing test names are changed to disguise a failure delta.

## Initial Runs And CI Disclosure

The initial broad runs included `tests/integration`, `tests/replay`, and
`tests/backtest`; they are NOT the requested full UNIT pair. The clean broad
baseline reported 6483 passed, 56 failed, 12 errors, two skipped, one xfailed
(`/tmp/orbpurple1-base-clean.log`, `.xml`). Its integration errors lacked the
PostgreSQL CI service. The initial broad head reported 6486 passed, 83 failed,
12 errors with the same skips (`/tmp/orbpurple1-head.log`, `.xml`); its 27
additional failures were the missing new flag's catalog registration.
Interrupted broad reruns are not verification evidence. An earlier baseline
run overlapped source edits and is also discarded.

The first exact UNIT head reported 6445 passed, 48 failed
(`/tmp/orbpurple1-unit-head.log`); its only added failure was the remaining
151-versus-149 catalog assertion, subsequently refreshed. Its failed-name
comparison is `/tmp/orbpurple1-unit-pair-first.json`.

Initial CI at `8f5918098f53d7416f8a7839c7b5acc6e2159d9f` failed on the
missing catalog registration, including 27 failed / 6466 passed in push
Validate run 37484762266. Raw failure log:
`/tmp/orbpurple1-ci-initial-failed.log`. This is explicitly not a green run.
Final exact-head Validate runs and their complete job results will be linked
in the PR after this evidence commit. Readiness requires BOTH runs green
and the full UNIT failed/error name sets unchanged from the base.
An independent review pin is separate, belongs to the reviewer, and is not
claimed complete by this lane.

The existing lint-control tests create transient selfcheck files and delete
them in `finally` blocks. No temporary selfcheck file belongs in the final
commit. No shared C-row/handoff edits or production writes, environment edits,
restart, deployment, ledger edit, or merge were performed.
