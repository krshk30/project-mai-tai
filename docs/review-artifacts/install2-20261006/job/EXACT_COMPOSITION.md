# Codex External Exact Install2 Controls

Frozen application `aed1a358c0da4043ce248550c2b1d02cecdc76ff`, tree
`b28df7b3d492d0be4e72e1233ce0fff55e43fca4`. No application edits, skips,
xfails, production reads or actions. This does not bind an eventual merge SHA.
Parent owns cumulative derivation repair; none of those files changed here.

## Measured Results

- External exact-setting suite: **102 passed, 0 failed/error/skipped, 6.71s**.
- Unchanged normal five-file suite: **307 passed, 14.06s**.
- Unchanged legacy ALL_ON suite, without forcing plugin: **41 passed, 2.90s**.
- Parent's original forced legacy run remains **33 pass / 8 fail**. It is not
  relabeled PASS. All eight names map precisely in EXACT_COMPOSITION_RECEIPTS.json.

The exact suite preserves the original 33 non-conflicting behavioral cases as
direct calls with the same clocks/prices/transports and controlled completed-line
prerequisite. It replaces six NFQ-token assumptions with actual reviewed MIRROR
hold/transfer/RPG nonce controls, and two untaken BUY expiry/latch expectations
with KEEP frozen-wait, taken-cross and terminal-cleanup controls. Old OFF/default
assertions remain unmodified and were exercised in the normal suites.

## Exact Execution

Run from the frozen candidate worktree, not the runner worktree: pytest's source
and `tests.unit` namespaces must resolve to the candidate. The suite asserts
actual HEAD/TREE and clean candidate `src`, `ops` and `tests` before exercising it.

```sh
env PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/Users/velkris/.codex/worktrees/ownmix1-rpgstuck1-evening-plan/project-mai-tai/docs/review-artifacts/install2-20261006:src:. \
  /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest \
  -c pyproject.toml --rootdir=. -p no:cacheprovider -p combination_plugin \
  --import-mode=importlib \
  /Users/velkris/.codex/worktrees/install2-runner-20261006/project-mai-tai/docs/review-artifacts/install2-20261006/job/test_exact_install2_composition.py \
  -q --tb=short --show-capture=no \
  --junitxml=/tmp/wbpower-install2-exact-composition.xml
```

Parent plugin forces four new true keys and retry=true/max0 at Settings init;
the external fixture retains all eight original ALL_ON values at that same
pre-cache boundary. Constructed settings, relevant cached strategy flags and
the actual Webull constructor's list-primary cache are checked.

The inherited OMS helper's permissive collision function is restored to the
actual implementation. An armed-other-owner negative control blocks SDK POST.
The SDK `__new__` fixture's historical list-primary OFF cache is restored to
the exact ON state with controlled reader/query runtime; actual constructor
wiring is separately exercised. No manufactured NFQ or RPG permission token:
positive queue and nonce identities come from real journals/hold machinery.
Changed/foreign token strings occur only as deliberate refusal controls.

## Scope And Limits

Actual retained-hold wire accounting, free waits, generation fencing, transfer,
unknown dispatch, late report, sibling versus own fills, both-broker partial
handoff fills, PM/RTH frozen waiting, exact later cross sizing, sell/window/gap/
session cleanup, removal barriers and list-primary accounting are controlled
behavioral PASS, not venue acceptance or historical-fill proof.

An initial gap-cleanup helper replaced only the gap reader with lambda True.
With restoration ON that revokes readiness before its legacy track branch.
The actual `begin_gap_hold` path was tested instead: it cancels frozen waits
before revoking readiness, including both owned RTH legs. The synthetic reader
alone is not claimed to be the real gap-transition protocol.

Completed-line output remains explicitly controlled, as in the retained suite;
no restoration mathematics/source repair is proved. SDK ACKs/fills, clock,
executable ask and future CLEAR terminal receipts are explicitly controlled.
No missing historical fields or broker funds are reconstructed. This is not a
complete factorial matrix, parent full unit pair/CI/mutation acceptance,
production process proof, cumulative-history fix or next-session delivery proof.

Raw JUnit paths and hashes, exact tested node names, plugin/suite hashes and
precise eight-failure mapping are retained in EXACT_COMPOSITION_RECEIPTS.json.
