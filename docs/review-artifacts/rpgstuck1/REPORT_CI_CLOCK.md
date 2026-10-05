# Restart fixture clock isolation

ONE fixture/tests/evidence-only follow-up atop
`8cfb704c4724ecfc846be11ba649464c965d9fb4`, no rebase. Exact main remains
`4987353be54c4be15d4196907dec4dd0d6103236`. Production, settings defaults,
ALL-ON catalog, recorded ticket payloads and prior L5/L7/APUS tests are unchanged.
No production/service/broker/ledger write, merge, install, restart, flags,
migration, shared handoff/plan or other/OWN worktree edit.

## Actual CI Failure And Cause

Push Validate `37366739332` on8cf acquired a runner and ran unit tests:
**5922 passed /3 failed**, at20:05:33Z (16:05:33ET). The failures are all
`test_current_rpg_off_startup_restores_proof_dependent_ticket_ownership`:

- `fbfd692d-ec1f-5a39-9e11-1a133abccc96`
- `bd6ac0b9-727c-500b-8581-aabdd95992d4`
- `a007716c-4b50-5759-a354-ddc5b8961154`

Each expected one primary and one mirror draft. The CI assertion proves only
primary0; it fails before proving the restarted mirror count. Mencius's independent
controlled restarted reproduction shows primary0/mirror0. The AMOD mirror logs
belong to the original setup, not restarted placement evidence.
Raw actual CI output is retained in `ci-clock-prior-push-failed.txt`. This is
an actual assertion failure, NOT the separate hosted-runner acquisition failure
on the old pull run. No test failure is waived as infrastructure.

`runtime(monkeypatch)` bound three session-clock methods on its ORIGINAL strategy.
The legacy test constructed a NEW restored `SchwabV2Strategy`, froze only its
`_now_ms`, and replayed recorded09:33/09:37 authorization time. The new instance's
`_resting_in_window` and `_resting_session_is_eh` still defaulted to
`datetime.now(UTC)` (strategy lines4923/4933). Their real16:00ET boundary correctly
closed the primary resting placement on the CI host, rather than replaying the
recorded RTH opportunity. Logs from the original runtime instance were not proof
that the new restored instance placed. `_entry_window_closed_for_session`
(line2625) already derives its default from `_now_ms`; binding it too makes the
restored fixture consistent with the existing three-method clock contract.

The correction adds ONLY four fixture lines after the restarted `_now_ms` bind:
capture each real bound predicate in `fn=method`, then supply the fixture clock
when no explicit `now` is given. No predicate is replaced with constant
True/False. All original legacy assertions, including proven/unproven ownership
and one draft per account, are unchanged. Explicit hostile time still goes to
the real gate and is rejected. This is a fixture wall-clock leak, not a
production defect; timezone differences are not the cause.

## Deterministic Regression And Falsification

New collected exact test:
`tests/unit/test_rpgstuck1_restart_clock.py::test_recorded_restart_real_session_gates_ignore_host_after_window`.
Its16 parameters are all four actual recorded tickets x hostTZ UTC/Eastern x
host16:05:33ET/20:05:33ET. It patches actual strategy `datetime.now`, constructs
the new instance through a capture wrapper, then calls the original legacy test
function with its unchanged assertions. It verifies the restarted timestamp is
recorded authorization/created_at+60s, all three real default session predicates
use that clock, and explicit hostile time still fails the real resting gates.
The TZ environment and libc timezone are restored in `finally`; production
protection and ownership checks are not disabled.

`check_restart_clock.py` produced this controlled matrix on Mac under both
`TZ=UTC` and `TZ=America/New_York`; actual Linux CI raw supplies the venue-host
failure evidence. This does not claim a local Linux run.

| Fixture and host time | UTC | Eastern |
| --- | --- | --- |
| Original fixture,19:44Z (15:44ET) | 4PASS | 4PASS |
| Original fixture,20:05:33Z (16:05ET) | same3FAIL/1PASS | same3FAIL/1PASS |
| Fixed fixture,19:44Z | 4PASS | 4PASS |
| Fixed fixture,20:05:33Z | 4PASS | 4PASS |
| Remove ONLY new clock binding in memory,19:44Z | 4PASS | 4PASS |
| Remove ONLY new clock binding in memory,20:05:33Z | same3 assertion-FAIL/1PASS | same3 assertion-FAIL/1PASS |

The same removal against the new16-case regression is **16 assertion-FAIL**.
The mutation preserves `_now_ms`, gate implementation and every original
assertion. It performs no file or production edit. Early classifier/import-path
prototypes were NOT_PROVEN and are excluded; valid reruns retained genuine
AssertionError and exact failed-test counts. No import/anchor error counts RED.

All prior **82 assertion-red control executions** were rerun fresh, including
literal R13/R16, R15/B4, R20, B5, terminal identity/fill guards, shared-connection
regression, PM6 and literal L5/L7. Clock binding supplies one new falsifier run
in two legacy host-TZ configurations plus the16-case regression: **85 RED
control executions**, not85 distinct guards. Before-fix reproduction and
before-cutoff PASS sanity checks are not counted as mutants. All matrix/raw
control files and SHA256 values are retained in `verification-ci-clock.json`.

## Frozen Verification

| Fresh suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Exact main498 full | 5577 | 56 | 0 |
| Fixture-only candidate full | 5885 | identical56 | 0 |
| Broad RPG/PM/OWN focused | 1571 | 0 | 0 |
| Restart regression + legacy module | 32 | 0 | 0 |

Exact full failed-name diff **added[] /removed[]**, XML parsed, not counts-only.
Main192.71s /candidate252.08s /focus103.62s /target2.66s. Full warnings310/315
and focused JUnit-property warnings are retained, not suppressed. Neither full
is globally green. All14 captured ALL-ON startup cases, actual SCKT Fill/no-rebuy,
configured-window zero opens, strict unknown ownership, first/reclaim,
R17-R20/B5, historical OFF and amended APUS later same-segment wire once remain
collected. No source change or favorable original-quote substitution.

`verification-ci-clock.json` retains both full XML/text files, complete failed
IDs and focused/targeted names, before/after fixture SHA256, new test hash,
unchanged production/settings/catalog/L5-L7 hashes and fresh control outputs.
The collector asserts production/catalog diff vs8cf is empty and hashes equal
the frozen L57 report. Source/tests were frozen before final full/focus runs.
Ruff src/tests/new evidence drivers PASS. Whitespace verification is scoped to
code/tests; verbatim raw evidence whitespace is not normalized.

```text
PYTHONPATH=src <repo-venv>/python -m pytest -q tests/unit --junitxml=/tmp/rpgstuck-ci-clock-main-full.xml
PYTHONPATH=src <repo-venv>/python -m pytest -q tests/unit --junitxml=/tmp/rpgstuck-ci-clock-head-full.xml
PYTHONPATH=src <repo-venv>/python -m pytest -q tests/unit/test_rpgstuck1_restart_clock.py tests/unit/test_pmprint1_tonight_flags.py --junitxml=/tmp/rpgstuck-ci-clock-targeted.xml
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_restart_clock.py after
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_restart_clock.py mutant
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_restart_clock.py regression-mutant
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/run_b_mutations.py
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_fixture_mutations.py
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_r20_mutations.py
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_all_on_proofs.py
PYTHONPATH=src <repo-venv>/python tests/unit/mutate_all_on_pm.py
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_l57_mutations.py
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/collect_ci_clock_results.py
```

The before-mode matrix was run before fixing the fixture. Full main used the
own detached exact498 worktree, no other lane was edited. Interpreter:
`/Users/velkris/Projects/project-mai-tai/.venv/bin/python`. The exact expanded
focused command is in verification JSON. Prior as-of14-ticket population and
controlled future quote/acceptance limits remain; no live whole-day certification.

New exact-head BOTH Validate SUCCESS and a fresh independent committed review
pin are required. Prior CI success and this local failed-name parity are not
install approval. Parent owns CI watching/reruns and plan/handoff; head freezes
after the single normal push. PR body may record later CI status without a
further source/test/report commit.
