# Linear NFQ2 stack on unchanged SLOTCLEAR1

PR1112 depends on PR1113 branch `codex/slotclear1-fresh-flip-side`, exact
9951b473f8203590f48c667770c07e1f9c3096b6 (unchanged). No merge commit is published.
Own linear worktree is `/tmp/codex-nfq2-on-slotclear1-linear-20261007`.
The internal merge proof remains a separate backup, not the published history.

Three NFQ markers were truly rebased from f9c9bd33 onto9951:
0239726f=81670de1,16c435b0!a8519b5d,0318ef84=6a6f5005.
The entry-path marker's only adaptations are the six preserved source/test
conflicts and combined consumer totals. The assessment and off-loop marker
patches are unchanged. `RANGE_DIFF.txt` is the complete measured range-diff;
`REBASE_CONFLICTS.diff` preserves the six conflict files. Four original
proof/joint-test commits follow linearly, ending2b4e861b; receipt-only commits
may follow. No RPGSTALE, RETRYLEFT, production, handoff or install changes.

`verify_equivalence.py` requires zero merges above9951 and byte-identical
src/tests/ops versus proof freeze5d77d48f213203d23ee2ef6d8ac354655ecc0e83.
All three subtree object IDs match. The NFQ range adds only NFQ runtime,
compatibility/catalog adjustments, joint tests and proof documentation above
the unchanged SLOT dependency. Diff is18 src/test/ops files,1676 insertions,
28 deletions. SLOT fresh BUY/SELL source is inherited, not repatched here.

Actual completed proof receipts retain their real head/path; they are not
relabeled as execution on this linear head:

- Broad focused446 pass/4 sustained deselected on f855; joint21 pass on final
  proof freeze5d77 after its import-only fixture repair.
- Restored current-ticket NFQ-enabled controls2 pass on5d77: both SXTC RPG
  owners veto19 real probes, zero opens, payloads unchanged. Empty-book replay
  is counterfactual, NOT actual both-leg placement. DKI retains its cap.
- Mutations45/45 RED: S1-S7, F1-F11, T1, N1-N17, C1-C3 and six combined
  quote-path controls; every failed node/raw output is preserved.
- Enabled-drift full quote handler with active hold, NFQ and both SLOT flags ON:
  14,400 events/60.000410s,239.998360/s; maximum stall29.085500ms<50ms;
  max handler16.177208ms. Every second >=200/s, zero tick SQL/sessions/tx and
  zero on-loop SQL. Explicit5s periodic work24 worker transactions/84 SQL.
  One held hold, no phase violations/retry; drift14,400 reads/7,200 checks.
  Exact gate19:52:56.970434--19:53:56.970972UTC, October7.

The active gate uses real SQLite ORM/SQL, fake Redis and simulated broker,
fixed logical NOW/AM with real60s pacing, no exit positions. HOTFIX drift
reader/cache remains enabled at1cent tolerance, not stubbed. Only explicit
NFQ proof/cache periodic turns run, not the full broker-sync loop. Eligible
save control sleeps150ms; serial claim/pipeline SQL outside the measured tick
gate still exists on-loop. No PostgreSQL/broker/live OMS qualification.
Full environment/hash/flag/stub inventory is in
`../nfq2-hotfix1-proof/slot-composed-benchmark-01/environment.json`.

The standard full unit rerun on identical frozen5d77 started20:04UTC, bounded
780s, command exactly matches retained main994 baseline. It remains PENDING
at initial publication. The earlier19:54:34--20:01:51 run was stopped ONLY in
the owned process group at62% to avoid parent overlap; raw output/return-15
is preserved and not a full PASS. No concurrency cause is asserted. Prior
four catalog failures, original SLOT58-fail launcher receipt, old drift-on
failure and parent HOTFIX watchdog failure remain unwaived.

Only PR1112 is published using human-authorized force-with-lease expecting
remote0318; backup branch/bundle are in CLAIM. PR1113 stays9951 with its own
prior green CI. Old CI is NOT claimed for the new linear head. New-head CI,
completed full failed-name pair and independent fresh review are pending.

NFQ uncertain-wire terminal recovery, legacy-unbound disposition and broader
delayed feedback/cancel/simultaneous-restart edges remain unproven. Historical
SXTC owner clearance and actual placement are unproved. This is scoped
composition proof, NOT NFQ ready. No pin, activation or main merge.

Reproduce with a fresh label/folder (existing receipts refuse overwrite):

```sh
export PYTHONPATH=src:.
/Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/nfq2-slotclear-linear/verify_equivalence.py
/Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest tests/unit/test_nfq2_slotclear_composition.py docs/review-artifacts/slotclear1/test_both_paths.py docs/review-artifacts/nfq2-slotclear-integration/test_restored_tickets_joint.py -q --tb=short
/Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/nfq2-hotfix1-proof/run_proof.py benchmark --label fresh-linear-benchmark
/Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/nfq2-hotfix1-proof/run_proof.py controls --label fresh-linear-controls
PROOF_TIMEOUT_SECONDS=780 PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/slotclear1/run_receipt.py "$PWD" docs/review-artifacts/nfq2-slotclear-linear/fresh-full /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest tests/unit -q --tb=short -vv
```
